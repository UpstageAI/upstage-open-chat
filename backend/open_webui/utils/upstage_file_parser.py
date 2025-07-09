import asyncio
import json
import logging
import aiohttp
import requests
from typing import Optional, List, Dict, Any
from fastapi import Request
from bs4 import BeautifulSoup
from langchain_core.documents import Document

from open_webui.utils.misc import calculate_sha256_string
from open_webui.routers.retrieval import save_docs_to_vector_db

log = logging.getLogger(__name__)


async def chat_file_parsing_handler(
    request: Request, form_data: dict, extra_params: dict, user
):
    from open_webui.retrieval.upstage_parser import wait_for_async_result_with_progress, download_and_merge_results
    from open_webui.models.files import Files
    from langchain_core.documents import Document

    event_emitter = extra_params["__event_emitter__"]
    await event_emitter({
        "type": "status",
        "data": {
            "action": "file_parsing",
            "description": "Waiting for document parsing results",
            "done": False,
        },
    })

    updated_files = []
    files = form_data.get("files", [])
    print(files)

    for file in files:
        file_id = file.get("id")
        file_info = file.get("file", {})
        request_id = file_info.get("meta", {}).get("request_id")

        # DB에서 file fetch
        file_record = Files.get_file_by_id(file_id)
        parsed_data = file_record.data

        collection_name = form_data.get("collection_name", None)

        if collection_name is None:
            collection_name = f"file-{file_id}"

        print(file_info)
        print(parsed_data)
        print(request_id)

        if not parsed_data and request_id:
            try:
                await event_emitter({
                    "type": "status",
                    "data": {
                        "action": "file_parsing",
                        "description": f"Parsing: {file_info.get('filename', '')}",
                        "done": False,
                    },
                })

                print(request.app.state.config.RAG_UPSTAGE_API_KEY)
                metadata = await wait_for_async_result_with_progress(
                    request_id,
                    request.app.state.config.RAG_UPSTAGE_API_KEY,
                    event_emitter,
                )
                merged_html = await download_and_merge_results(
                    metadata,
                )
                print(merged_html)

                docs = [
                    Document(
                        page_content=merged_html,
                        metadata={
                            "name": file_info["filename"],
                            "created_by": file_info["user_id"],
                            "file_id": file_id,
                            "source": file_info["filename"],
                        },
                    )
                ]

                text_content = " ".join([doc.page_content for doc in docs])
                log.debug(f"text_content: {text_content[:200]}...")

                await event_emitter({
                    "type": "status",
                    "data": {
                        "action": "file_parsing",
                        "description": f"Embedding: {file_info.get('filename', '')}",
                        "done": True,
                    },
                })

                Files.update_file_data_by_id(file_id, {"content": text_content})
                content_hash = calculate_sha256_string(text_content)
                Files.update_file_hash_by_id(file_id, content_hash)

                # 파일 크기에 따라 임베딩 우회 여부 결정
                text_content_size = len(text_content.encode('utf-8'))
                auto_bypass_threshold = request.app.state.config.AUTO_BYPASS_FILE_EMBEDDING_SIZE_THRESHOLD
                manual_setting = request.app.state.config.BYPASS_EMBEDDING_AND_RETRIEVAL
                
                if manual_setting is True:
                    should_bypass = True
                else:
                    should_bypass = text_content_size <= auto_bypass_threshold
                
                log.info(f"Parsed file content size: {text_content_size} bytes, threshold: {auto_bypass_threshold} bytes, bypass: {should_bypass}")
                
                if not should_bypass:
                    result = save_docs_to_vector_db(
                        request,
                        docs=docs,
                        collection_name=collection_name,
                        metadata={
                            "file_id": file_id,
                            "name": file_info["filename"],
                            "hash": content_hash,
                        },
                        add=bool(collection_name),
                        user=user,
                    )

                    if result:
                        Files.update_file_metadata_by_id(
                            file_id,
                            {"collection_name": collection_name},
                        )

                # 파일 정보 최신화
                file["file"] = dict(Files.get_file_by_id(file_id))

                await event_emitter({
                    "type": "status",
                    "data": {
                        "action": "file_parsing",
                        "description": f"Parsed: {file_info.get('filename', '')}",
                        "done": True,
                    },
                })

            except Exception as e:
                log.exception(f"Failed to parse {file_info.get('filename', '')}: {e}")
                await event_emitter({
                    "type": "status",
                    "data": {
                        "action": "file_parsing",
                        "description": f"Failed: {file_info.get('filename', '')}",
                        "done": True,
                        "error": True,
                    },
                })

        updated_files.append(file)

    form_data["files"] = updated_files

    await event_emitter({
        "type": "status",
        "data": {
            "action": "file_parsing",
            "description": "Parsed using Upstage’s Document Parser!",
            "done": True,
        },
    })

    return form_data
