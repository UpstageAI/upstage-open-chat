import json
import logging
import requests
from typing import Optional, List

from langchain_core.documents import Document
from open_webui.models.users import UserModel
from open_webui.config import RAG_EMBEDDING_PREFIX_FIELD_NAME
from open_webui.env import ENABLE_FORWARD_USER_INFO_HEADERS

log = logging.getLogger(__name__)

def generate_upstage_document_parsing(
    model: str,
    file_path: str,
    url: str = "https://api.upstage.ai/v1",
    key: str = "",
    ocr: str = "auto",
    chart_recognition: bool = True,
    coordinates: bool = True,
    output_formats: list[str] = ["markdown"],
    base64_encoding: list[str] = ["figure"],
    prefix: str = None,
    user: UserModel = None,
) -> Optional[list[list[float]]]:
    import json
    try:
        # json_data = {"input": texts, "model": model}
        files = {"document": open(file_path, "rb")}
        # print(files)
        data = {
            "model": model,
            "ocr": ocr,
            "chart_recognition": chart_recognition,
            "coordinates": coordinates,
            "output_formats": json.dumps(output_formats),
            "base64_encoding": json.dumps(base64_encoding),
        }
        # print(data)
        # if isinstance(RAG_EMBEDDING_PREFIX_FIELD_NAME, str) and isinstance(prefix, str):
        #     json_data[RAG_EMBEDDING_PREFIX_FIELD_NAME] = prefix

        r = requests.post(
            f"{url}/document-digitization",
            headers={
                # "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                # **(
                #     {
                #         "X-OpenWebUI-User-Name": user.name,
                #         "X-OpenWebUI-User-Id": user.id,
                #         "X-OpenWebUI-User-Email": user.email,
                #         "X-OpenWebUI-User-Role": user.role,
                #     }
                #     if ENABLE_FORWARD_USER_INFO_HEADERS and user
                #     else {}
                # ),
            },
            files=files,
            data=data,
        )
        r.raise_for_status()
        data = r.json()
        # print(data)
        
        # content 필드 확인 및 여러 포맷 지원
        if "content" in data:
            content = data["content"]
            
            # 여러 포맷 확인 (markdown 우선, html 후순위)
            if isinstance(content, dict):
                if "markdown" in content and content["markdown"]:
                    return [
                        Document(
                            page_content=content["markdown"], metadata={}
                        )
                    ]
                elif "html" in content and content["html"]:
                    return [
                        Document(
                            page_content=content["html"], metadata={}
                        )
                    ]
        
        # 데이터가 있지만 예상 구조가 아닐 때
        if data:
            log.warning(f"Unexpected Upstage API response structure. Data keys: {list(data.keys())}")
            raise Exception(f"Unexpected API response structure. Expected 'content' field with 'markdown' or 'html'")
        else:
            raise Exception("Empty response from Upstage API")
    except Exception as e:
        log.exception(f"Error generating upstage document parsing: {e}")
        return None


def generate_upstage_document_parsing_async(
    model: str,
    file_path: str,
    url: str = "https://api.upstage.ai/v1",
    key: str = "",
    ocr: str = "auto",
    chart_recognition: bool = True,
    coordinates: bool = True,
    output_formats: list[str] = ["markdown"],
    base64_encoding: list[str] = ["figure"],
) -> str:
    import json
    try:
        # json_data = {"input": texts, "model": model}
        files = {"document": open(file_path, "rb")}
        # print(files)
        data = {
            "model": model,
            "ocr": ocr,
            "chart_recognition": chart_recognition,
            "coordinates": coordinates,
            "output_formats": json.dumps(output_formats),
            "base64_encoding": json.dumps(base64_encoding),
        }
        # print(data)
        # if isinstance(RAG_EMBEDDING_PREFIX_FIELD_NAME, str) and isinstance(prefix, str):
        #     json_data[RAG_EMBEDDING_PREFIX_FIELD_NAME] = prefix

        r = requests.post(
            f"{url}/document-digitization/async",
            headers={
                # "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            files=files,
            data=data,
        )
        r.raise_for_status()
        data = r.json()
        # print(data)
        if "request_id" in data:
            return data["request_id"]
        else:
            raise Exception("Failed to get request_id from Upstage API response")
    except Exception as e:
        log.exception(f"Error generating upstage document parsing async: {e}")
        return None

import aiohttp
import asyncio

async def wait_for_async_result_with_progress(
        request_id: str, 
        key: str,
        event_emitter=None, 
        poll_interval: int = 2, 
        timeout: int = 180,):
    url = f"https://api.upstage.ai/v1/document-digitization/requests/{request_id}"
    headers = {"Authorization": f"Bearer {key}"}

    start_time = asyncio.get_event_loop().time()

    async with aiohttp.ClientSession() as session:
        while True:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    raise Exception(f"Failed to fetch async result: {resp.status}")

                result = await resp.json()
                status = result.get("status", "unknown")
                total_pages = result.get("total_pages", 0)
                completed_pages = result.get("completed_pages", 0)

                if event_emitter:
                    await event_emitter(
                        {
                            "type": "status",
                            "data": {
                                "action": "file_parsing",
                                "description": f"Parsing {status}... completed {completed_pages} of {total_pages} pages",
                                "done": status == "completed",
                            },
                        }
                    )

                if status == "completed":
                    return result
                elif status == "failed":
                    raise Exception(result.get("failure_message", "Unknown failure"))

                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > timeout:
                    raise TimeoutError("Document parsing timed out")

                await asyncio.sleep(poll_interval)

import json
from bs4 import BeautifulSoup
import aiohttp  # 확인용

async def download_and_merge_results(result_metadata):
    batches = result_metadata.get("batches", [])
    all_html_parts = []
    all_markdown_parts = []

    def sync_download_batch(url):
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "*/*",
            "Referer": "https://www.google.com"
        }
        resp = requests.get(url, headers=headers)
        if resp.status_code == 200:
            return resp.json()
        else:
            raise Exception(f"Failed to download batch: {resp.status_code} - {resp.text[:300]}")

    # Authorization 헤더 제거, User-Agent만 유지

    for batch in batches:
        download_url = batch.get("download_url")
        if not download_url:
            continue

        print(f"⬇️ Downloading batch {batch['id']} from {download_url}")
        data = sync_download_batch(download_url)

        # html_content = data.get("content", {}).get("html", "")
        # if html_content:
        #     soup = BeautifulSoup(html_content, "html.parser")
        #     all_html_parts.append(soup)
        markdown_content = data.get("content", {}).get("markdown", "")
        if markdown_content:
            all_markdown_parts.append(markdown_content)
    merged_markdown = ""
    for part in all_markdown_parts:
        merged_markdown += part + "\n\n"
    
    return merged_markdown
    # merged_html = "<html><body>"
    # for soup in all_html_parts:
    #     merged_html += str(soup) + "<hr/>"
    # merged_html += "</body></html>"

    # return merged_html



def generate_upstage_batch_embeddings(
    model: str,
    texts: list[str],
    url: str = "https://api.upstage.ai/v1",
    key: str = "",
    prefix: str = None,
    user: UserModel = None,
) -> Optional[list[list[float]]]:
    try:
        json_data = {"input": texts, "model": model}
        if isinstance(RAG_EMBEDDING_PREFIX_FIELD_NAME, str) and isinstance(prefix, str):
            json_data[RAG_EMBEDDING_PREFIX_FIELD_NAME] = prefix

        r = requests.post(
            f"{url}/embeddings",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                **(
                    {
                        "X-OpenWebUI-User-Name": user.name,
                        "X-OpenWebUI-User-Id": user.id,
                        "X-OpenWebUI-User-Email": user.email,
                        "X-OpenWebUI-User-Role": user.role,
                    }
                    if ENABLE_FORWARD_USER_INFO_HEADERS and user
                    else {}
                ),
            },
            json=json_data,
        )
        r.raise_for_status()
        data = r.json()
        if "data" in data:
            return [elem["embedding"] for elem in data["data"]]
        else:
            raise Exception("Failed to get embedding data from Upstage API response")
    except Exception as e:
        log.exception(f"Error generating openai batch embeddings: {e}")
        return None