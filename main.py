from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import requests
import os
from openai import AzureOpenAI
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
import logging
from typing import Any, Dict

# Load environment variables
load_dotenv()
logging.basicConfig(level=logging.INFO)

TENANT_ID = os.getenv("TENANT_ID")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
D365_ENV_URL = os.getenv("D365_ENV_URL")

# === FastAPI Setup ===
app = FastAPI()
# Attempt to use an external MCP library if available; otherwise use LocalMCP fallback.
try:
    import mcp  # type: ignore
    MCP_AVAILABLE = True
    logging.info("Using external 'mcp' library for MCP operations.")
except Exception:
    MCP_AVAILABLE = False
    logging.info("'mcp' library not found; using LocalMCP fallback implementation.")

class LocalMCP:
    """
    Lightweight MCP fallback that:
    - generates simple OData paths from an intent string
    - extracts customer names and ids from an OData JSON response
    This is intentionally conservative and deterministic.
    """
    @staticmethod
    def generate_odata_query(intent: str) -> str:
        intent_lower = intent.lower()
        # Very small set of heuristics for common customer queries
        if "top" in intent_lower and "customers" in intent_lower:
            # extract a number if provided
            import re
            m = re.search(r'\btop\s+(\d+)\b', intent_lower)
            top_n = m.group(1) if m else "10"
            return f"CustomersV3?$top={top_n}"
        if "customers" in intent_lower or "get customers" in intent_lower:
            return "CustomersV3?$top=50"
        # default safe path
        return "CustomersV3?$top=10"

    @staticmethod
    def process_odata_response(raw: Any) -> str:
        """
        Accepts the JSON decoded response from call_odata and returns
        a compact string listing customer IDs and names (or a fallback message).
        """
        try:
            # raw is expected to be dict resulting from response.json()
            if isinstance(raw, dict) and "value" in raw and isinstance(raw["value"], list):
                rows = raw["value"]
                results = []
                for r in rows:
                    cust_id = r.get("PartyNumber") or r.get("CustomerAccount") or r.get("AccountNumber") or r.get("Id") or r.get("RecId")
                    name = r.get("Name") or r.get("PartyLegalName") or r.get("DisplayName") or r.get("PartyName") or r.get("CustomerName")
                    results.append(f"{cust_id or 'unknown'}: {name or 'unknown'}")
                return "\n".join(results) if results else "No customers found in response."
            # if raw is a string (XML returned as text) then return as-is
            if isinstance(raw, str):
                return raw
            return str(raw)
        except Exception as e:
            logging.exception("Error processing OData response")
            return f"Error processing response: {e}"

# === FastAPI Setup ===
app = FastAPI()

# === Request and Response Models ===
class McpRequest(BaseModel):
    context: str

class McpResponse(BaseModel):
    message: str
    processedContext: str

# === Azure AD Token for D365 F&O ===
def get_access_token() -> str:
    token_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/token"
    payload = {
        'grant_type': 'client_credentials',
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'resource': D365_ENV_URL
    }

    response = requests.post(token_url, data=payload)
    if response.status_code == 200:
        return response.json().get('access_token')
    else:
        raise Exception(f"Token request failed: {response.status_code} - {response.text}")

# === MCP-based replacements for LLM calls ===
def query_mcp_for_odata(intent: str) -> str:
    if MCP_AVAILABLE:
        # expected external library API:
        # query = mcp.generate_odata_query(intent)
        # adapt to your mcp package API if different
        try:
            return mcp.generate_odata_query(intent)
        except Exception as e:
            logging.exception("External mcp.generate_odata_query failed, falling back to LocalMCP")
    return LocalMCP.generate_odata_query(intent)

def query_mcp_for_changes(raw_response: Any) -> str:
    if MCP_AVAILABLE:
        try:
            return mcp.process_odata_response(raw_response)
        except Exception as e:
            logging.exception("External mcp.process_odata_response failed, falling back to LocalMCP")
    return LocalMCP.process_odata_response(raw_response)

# === Call D365 F&O OData Endpoint ===
def call_odata(query_path: str) -> Dict:
    base_url = f"{D365_ENV_URL}/data"
    full_url = f"{base_url}/{query_path}"

    headers = {
        "Authorization": f"Bearer {get_access_token()}",
        "Accept": "application/json"
    }
    logging.info(f"Calling OData endpoint: {full_url}")
    response = requests.get(full_url, headers=headers)
    if response.status_code == 200:
        try:
            return response.json()
        except ValueError:
            # if not JSON (e.g., XML or plain text), return raw text
            return {"_raw_text": response.text}
    else:
        return {"error": response.text, "status_code": response.status_code}

mcp = FastMCP("D365 FO Customer")   
@mcp.tool()
def mcp_tool(context: str) -> McpResponse:
    try:
        # Step 1: Generate OData query path from intent
        odata_query = query_mcp_for_odata(context)
        logging.info(f"Generated OData query: {odata_query}")

        # Step 2: Call the OData endpoint
        odata_response = call_odata(odata_query)
        logging.info(f"OData response received")

        # Step 3: Process the OData response to extract relevant info
        processed_context = query_mcp_for_changes(odata_response)
        logging.info(f"Processed context generated")

        # Step 4: Create a response message
        message = f"Hello, here is the information you requested:\n{processed_context}"

        return McpResponse(message=message, processedContext=processed_context)
    except Exception as e:
        logging.exception("Error in MCP tool processing")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    mcp.run()
