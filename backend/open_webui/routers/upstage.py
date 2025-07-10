import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Literal, Optional, overload

import aiohttp
from aiocache import cached
import requests


from fastapi import Depends, FastAPI, HTTPException, Request, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from open_webui.models.models import Models
from open_webui.config import (
    CACHE_DIR,
)
from open_webui.env import (
    AIOHTTP_CLIENT_TIMEOUT,
    AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST,
    ENABLE_FORWARD_USER_INFO_HEADERS,
    BYPASS_MODEL_ACCESS_CONTROL,
)
from open_webui.models.users import UserModel

from open_webui.constants import ERROR_MESSAGES
from open_webui.env import ENV, SRC_LOG_LEVELS


from open_webui.utils.payload import (
    apply_model_params_to_body_openai,
    apply_model_system_prompt_to_body,
)
from open_webui.utils.misc import (
    convert_logit_bias_input_to_json,
)

from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.access_control import has_access


log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["UPSTAGE"])


##########################################
#
# Utility functions
#
##########################################


async def send_get_request(url, key=None, user: UserModel = None):
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(
                url,
                headers={
                    **({"Authorization": f"Bearer {key}"} if key else {}),
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
            ) as response:
                return await response.json()
    except Exception as e:
        # Handle connection error here
        log.error(f"Connection error: {e}")
        return None

def get_model_list(idx):
    
    model_list = {
        "object": "list",
        "data": [
            {
                "id": model_id,
                "name": model_id,
                "owned_by": "upstage",
                "upstage": {"id": model_id},
                "urlIdx": idx,
            }
            for model_id in [
                "solar-pro2",
                "solar-mini"
            ]
        ],
    }
    return model_list


async def cleanup_response(
    response: Optional[aiohttp.ClientResponse],
    session: Optional[aiohttp.ClientSession],
):
    if response:
        response.close()
    if session:
        await session.close()


# def openai_o1_o3_handler(payload):
#     """
#     Handle o1, o3 specific parameters
#     """
#     if "max_tokens" in payload:
#         # Remove "max_tokens" from the payload
#         payload["max_completion_tokens"] = payload["max_tokens"]
#         del payload["max_tokens"]

#     # Fix: o1 and o3 do not support the "system" role directly.
#     # For older models like "o1-mini" or "o1-preview", use role "user".
#     # For newer o1/o3 models, replace "system" with "developer".
#     if payload["messages"][0]["role"] == "system":
#         model_lower = payload["model"].lower()
#         if model_lower.startswith("o1-mini") or model_lower.startswith("o1-preview"):
#             payload["messages"][0]["role"] = "user"
#         else:
#             payload["messages"][0]["role"] = "developer"

#     return payload


##########################################
#
# API routes
#
##########################################

router = APIRouter()


@router.get("/config")
async def get_config(request: Request, user=Depends(get_admin_user)):
    return {
        "ENABLE_UPSTAGE_API": request.app.state.config.ENABLE_UPSTAGE_API, # Always true for upstage
        "UPSTAGE_API_BASE_URLS": request.app.state.config.UPSTAGE_API_BASE_URLS,
        "UPSTAGE_API_KEYS": request.app.state.config.UPSTAGE_API_KEYS,
        "UPSTAGE_API_CONFIGS": request.app.state.config.UPSTAGE_API_CONFIGS,
    }


class UpstageConfigForm(BaseModel):
    ENABLE_UPSTAGE_API: Optional[bool] = None
    UPSTAGE_API_BASE_URLS: list[str]
    UPSTAGE_API_KEYS: list[str]
    UPSTAGE_API_CONFIGS: dict


@router.post("/config/update")
async def update_config(
    request: Request, form_data: UpstageConfigForm, user=Depends(get_admin_user)
):
    request.app.state.config.ENABLE_UPSTAGE_API = form_data.ENABLE_UPSTAGE_API
    request.app.state.config.UPSTAGE_API_BASE_URLS = form_data.UPSTAGE_API_BASE_URLS
    request.app.state.config.UPSTAGE_API_KEYS = form_data.UPSTAGE_API_KEYS

    # Check if API KEYS length is same than API URLS length
    if len(request.app.state.config.UPSTAGE_API_KEYS) != len(
        request.app.state.config.UPSTAGE_API_BASE_URLS
    ):
        if len(request.app.state.config.UPSTAGE_API_KEYS) > len(
            request.app.state.config.UPSTAGE_API_BASE_URLS
        ):
            request.app.state.config.UPSTAGE_API_KEYS = (
                request.app.state.config.UPSTAGE_API_KEYS[
                    : len(request.app.state.config.UPSTAGE_API_BASE_URLS)
                ]
            )
        else:
            request.app.state.config.UPSTAGE_API_KEYS += [""] * (
                len(request.app.state.config.UPSTAGE_API_BASE_URLS)
                - len(request.app.state.config.UPSTAGE_API_KEYS)
            )

    request.app.state.config.UPSTAGE_API_CONFIGS = form_data.UPSTAGE_API_CONFIGS

    # Remove the API configs that are not in the API URLS
    keys = list(map(str, range(len(request.app.state.config.UPSTAGE_API_BASE_URLS))))
    request.app.state.config.UPSTAGE_API_CONFIGS = {
        key: value
        for key, value in request.app.state.config.UPSTAGE_API_CONFIGS.items()
        if key in keys
    }

    return {
        "ENABLE_UPSTAGE_API": request.app.state.config.ENABLE_UPSTAGE_API,
        "UPSTAGE_API_BASE_URLS": request.app.state.config.UPSTAGE_API_BASE_URLS,
        "UPSTAGE_API_KEYS": request.app.state.config.UPSTAGE_API_KEYS,
        "UPSTAGE_API_CONFIGS": request.app.state.config.UPSTAGE_API_CONFIGS,
    }


@router.post("/audio/speech")
async def speech(request: Request, user=Depends(get_verified_user)):
    idx = None
    try:
        idx = request.app.state.config.UPSTAGE_API_BASE_URLS.index(
            "https://api.upstage.ai/v1"
        )

        body = await request.body()
        name = hashlib.sha256(body).hexdigest()

        SPEECH_CACHE_DIR = CACHE_DIR / "audio" / "speech"
        SPEECH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        file_path = SPEECH_CACHE_DIR.joinpath(f"{name}.mp3")
        file_body_path = SPEECH_CACHE_DIR.joinpath(f"{name}.json")

        # Check if the file already exists in the cache
        if file_path.is_file():
            return FileResponse(file_path)

        url = request.app.state.config.UPSTAGE_API_BASE_URLS[idx]

        r = None
        try:
            r = requests.post(
                url=f"{url}/audio/speech",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {request.app.state.config.UPSTAGE_API_KEYS[idx]}",
                    **(
                        {
                            "HTTP-Referer": "https://openwebui.com/",
                            "X-Title": "Open WebUI",
                        }
                        if "openrouter.ai" in url
                        else {}
                    ),
                    **(
                        {
                            "X-OpenWebUI-User-Name": user.name,
                            "X-OpenWebUI-User-Id": user.id,
                            "X-OpenWebUI-User-Email": user.email,
                            "X-OpenWebUI-User-Role": user.role,
                        }
                        if ENABLE_FORWARD_USER_INFO_HEADERS
                        else {}
                    ),
                },
                stream=True,
            )

            r.raise_for_status()

            # Save the streaming content to a file
            with open(file_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)

            with open(file_body_path, "w") as f:
                json.dump(json.loads(body.decode("utf-8")), f)

            # Return the saved file
            return FileResponse(file_path)

        except Exception as e:
            log.exception(e)

            detail = None
            if r is not None:
                try:
                    res = r.json()
                    if "error" in res:
                        detail = f"External: {res['error']}"
                except Exception:
                    detail = f"External: {e}"

            raise HTTPException(
                status_code=r.status_code if r else 500,
                detail=detail if detail else "Open WebUI: Server Connection Error",
            )

    except ValueError:
        raise HTTPException(status_code=401, detail=ERROR_MESSAGES.UPSTAGE_NOT_FOUND)


async def get_all_models_responses(request: Request, user: UserModel) -> list:
    if not request.app.state.config.ENABLE_UPSTAGE_API:
        return []
    
    print(request.app.state.config.UPSTAGE_API_CONFIGS)

    # Check if API KEYS length is same than API URLS length
    num_urls = len(request.app.state.config.UPSTAGE_API_BASE_URLS)
    num_keys = len(request.app.state.config.UPSTAGE_API_KEYS)

    if num_keys != num_urls:
        # if there are more keys than urls, remove the extra keys
        if num_keys > num_urls:
            new_keys = request.app.state.config.UPSTAGE_API_KEYS[:num_urls]
            request.app.state.config.UPSTAGE_API_KEYS = new_keys
        # if there are more urls than keys, add empty keys
        else:
            request.app.state.config.UPSTAGE_API_KEYS += [""] * (num_urls - num_keys)

    request_tasks = []
    for idx, url in enumerate(request.app.state.config.UPSTAGE_API_BASE_URLS):
        if url == "https://api.upstage.ai/v1":
            model_list = get_model_list(idx)

            api_config = request.app.state.config.UPSTAGE_API_CONFIGS.get(
                str(idx),
                request.app.state.config.UPSTAGE_API_CONFIGS.get(
                    url, {}
                ),  # Legacy support
            )

            prefix_id = api_config.get("prefix_id", None)
            tags = api_config.get("tags", [])

            if prefix_id:
                for model in (
                    model_list if isinstance(model_list, list) else model_list.get("data", [])
                ):
                    model["id"] = f"{prefix_id}.{model['id']}"

            if tags:
                for model in (
                    model_list if isinstance(model_list, list) else model_list.get("data", [])
                ):
                    model["tags"] = tags

            for model in (
                model_list if isinstance(model_list, list) else model_list.get("data", [])
            ):
                if model.get("name", "") == "solar-pro2":
                    model["reasoning_effort"] = True
            
            request_tasks.append(asyncio.ensure_future(asyncio.sleep(0, model_list)))

        elif (str(idx) not in request.app.state.config.UPSTAGE_API_CONFIGS) and (
            url not in request.app.state.config.UPSTAGE_API_CONFIGS  # Legacy support
        ):
            request_tasks.append(
                send_get_request(
                    f"{url}/models",
                    request.app.state.config.UPSTAGE_API_KEYS[idx],
                    user=user,
                )
            )
        else:
            api_config = request.app.state.config.UPSTAGE_API_CONFIGS.get(
                str(idx),
                request.app.state.config.UPSTAGE_API_CONFIGS.get(
                    url, {}
                ),  # Legacy support
            )

            enable = api_config.get("enable", True)
            model_ids = api_config.get("model_ids", [])

            if enable:
                if len(model_ids) == 0:
                    request_tasks.append(
                        send_get_request(
                            f"{url}/models",
                            request.app.state.config.UPSTAGE_API_KEYS[idx],
                            user=user,
                        )
                    )
                else:
                    model_list = {
                        "object": "list",
                        "data": [
                            {
                                "id": model_id,
                                "name": model_id,
                                "owned_by": "upstage",
                                "upstage": {"id": model_id},
                                "urlIdx": idx,
                            }
                            for model_id in model_ids
                        ],
                    }

                    request_tasks.append(
                        asyncio.ensure_future(asyncio.sleep(0, model_list))
                    )
            else:
                request_tasks.append(asyncio.ensure_future(asyncio.sleep(0, None)))

    responses = await asyncio.gather(*request_tasks)
    # responses = [get_model_list1(), get_model_list2()]

    # for idx, response in enumerate(responses):
    #     if response:
    #         url = request.app.state.config.UPSTAGE_API_BASE_URLS[idx]
    #         api_config = request.app.state.config.UPSTAGE_API_CONFIGS.get(
    #             str(idx),
    #             request.app.state.config.UPSTAGE_API_CONFIGS.get(
    #                 url, {}
    #             ),  # Legacy support
    #         )

    #         prefix_id = api_config.get("prefix_id", None)
    #         tags = api_config.get("tags", [])

    #         if prefix_id:
    #             for model in (
    #                 response if isinstance(response, list) else response.get("data", [])
    #             ):
    #                 model["id"] = f"{prefix_id}.{model['id']}"

    #         if tags:
    #             for model in (
    #                 response if isinstance(response, list) else response.get("data", [])
    #             ):
    #                 model["tags"] = tags

    # log.debug(f"get_all_models:responses() {responses}")
    print(responses)
    return responses


async def get_filtered_models(models, user):
    # Filter models based on user access control
    filtered_models = []
    for model in models.get("data", []):
        model_info = Models.get_model_by_id(model["id"])
        if model_info:
            if user.id == model_info.user_id or has_access(
                user.id, type="read", access_control=model_info.access_control
            ):
                filtered_models.append(model)
    return filtered_models


@cached(ttl=1)
async def get_all_models(request: Request, user: UserModel) -> dict[str, list]:
    log.info("get_all_models()")

    if not request.app.state.config.ENABLE_UPSTAGE_API:
        return {"data": []}

    responses = await get_all_models_responses(request, user=user)

    def extract_data(response):
        if response and "data" in response:
            return response["data"]
        if isinstance(response, list):
            return response
        return None

    def merge_models_lists(model_lists):
        log.debug(f"merge_models_lists {model_lists}")
        merged_list = []

        for idx, models in enumerate(model_lists):
            if models is not None and "error" not in models:

                merged_list.extend(
                    [
                        {
                            **model,
                            "name": model.get("name", model["id"]),
                            "owned_by": "upstage",
                            "upstage": model,
                            "urlIdx": idx,
                        }
                        for model in models
                    ]
                )

        return merged_list

    models = {"data": merge_models_lists(map(extract_data, responses))}
    log.debug(f"models: {models}")

    request.app.state.UPSTAGE_MODELS = {model["id"]: model for model in models["data"]}
    return models


@router.get("/models")
@router.get("/models/{url_idx}")
async def get_models(
    request: Request, url_idx: Optional[int] = None, user=Depends(get_verified_user)
):
    # models = {
    #     "data": [],
    # }

    # if url_idx is None:
    #     models = await get_all_models(request, user=user)
    # else:
    #     url = request.app.state.config.UPSTAGE_API_BASE_URLS[url_idx]
    #     key = request.app.state.config.UPSTAGE_API_KEYS[url_idx]

    #     r = None
    #     async with aiohttp.ClientSession(
    #         timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST)
    #     ) as session:
    #         try:
    #             async with session.get(
    #                 f"{url}/models",
    #                 headers={
    #                     "Authorization": f"Bearer {key}",
    #                     "Content-Type": "application/json",
    #                     **(
    #                         {
    #                             "X-OpenWebUI-User-Name": user.name,
    #                             "X-OpenWebUI-User-Id": user.id,
    #                             "X-OpenWebUI-User-Email": user.email,
    #                             "X-OpenWebUI-User-Role": user.role,
    #                         }
    #                         if ENABLE_FORWARD_USER_INFO_HEADERS
    #                         else {}
    #                     ),
    #                 },
    #             ) as r:
    #                 if r.status != 200:
    #                     # Extract response error details if available
    #                     error_detail = f"HTTP Error: {r.status}"
    #                     res = await r.json()
    #                     if "error" in res:
    #                         error_detail = f"External Error: {res['error']}"
    #                     raise Exception(error_detail)

    #                 response_data = await r.json()

    #                 # Check if we're calling OpenAI API based on the URL
    #                 if "api.upstage.ai" in url:
    #                     # Filter models according to the specified conditions
    #                     response_data["data"] = [
    #                         model
    #                         for model in response_data.get("data", [])
    #                         if not any(
    #                             name in model["id"]
    #                             for name in [
    #                                 "babbage",
    #                                 "dall-e",
    #                                 "davinci",
    #                                 "embedding",
    #                                 "tts",
    #                                 "whisper",
    #                             ]
    #                         )
    #                     ]

    #                 models = response_data
    #         except aiohttp.ClientError as e:
    #             # ClientError covers all aiohttp requests issues
    #             log.exception(f"Client error: {str(e)}")
    #             raise HTTPException(
    #                 status_code=500, detail="Open WebUI: Server Connection Error"
    #             )
    #         except Exception as e:
    #             log.exception(f"Unexpected error: {e}")
    #             error_detail = f"Unexpected error: {str(e)}"
    #             raise HTTPException(status_code=500, detail=error_detail)
    
    # models = get_model_list()
    models = await get_all_models(request, user)

    if user.role == "user" and not BYPASS_MODEL_ACCESS_CONTROL:
        models["data"] = await get_filtered_models(models, user)

    

    return models


class ConnectionVerificationForm(BaseModel):
    url: str
    key: str


@router.post("/verify")
async def verify_connection(
    form_data: ConnectionVerificationForm, user=Depends(get_admin_user)
):
    url = form_data.url
    key = form_data.key

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST)
    ) as session:
        try:
            async with session.get(
                f"{url}/models",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    **(
                        {
                            "X-OpenWebUI-User-Name": user.name,
                            "X-OpenWebUI-User-Id": user.id,
                            "X-OpenWebUI-User-Email": user.email,
                            "X-OpenWebUI-User-Role": user.role,
                        }
                        if ENABLE_FORWARD_USER_INFO_HEADERS
                        else {}
                    ),
                },
            ) as r:
                if r.status != 200:
                    # Extract response error details if available
                    error_detail = f"HTTP Error: {r.status}"
                    res = await r.json()
                    if "error" in res:
                        error_detail = f"External Error: {res['error']}"
                    raise Exception(error_detail)

                response_data = await r.json()
                return response_data

        except aiohttp.ClientError as e:
            # ClientError covers all aiohttp requests issues
            log.exception(f"Client error: {str(e)}")
            raise HTTPException(
                status_code=500, detail="Open WebUI: Server Connection Error"
            )
        except Exception as e:
            log.exception(f"Unexpected error: {e}")
            error_detail = f"Unexpected error: {str(e)}"
            raise HTTPException(status_code=500, detail=error_detail)


import base64
import tempfile

async def process_message_with_ocr(msg, api_key, message_content=None):
    try:
        url = "https://api.upstage.ai/v1/document-digitization"
        headers = {"Authorization": f"Bearer {api_key}"}
        
        image_data = msg.get("image_url", {}).get("url", "")
        if image_data.startswith("data:image"):
            # base64 decode
            header, b64data = image_data.split(",", 1)
            image_bytes = base64.b64decode(b64data)

            # save temporarily
            with tempfile.NamedTemporaryFile(suffix=".png") as temp_file:
                temp_file.write(image_bytes)
                temp_file.flush()

                # send to OCR
                files = {"document": open(temp_file.name, "rb")}
                data = {"model": "ocr"}
                response = requests.post(url, headers=headers, files=files, data=data)
                ocr_result = response.json()

                # 전체 confidence 체크
                confidence = ocr_result.get("confidence", 0)
                # log.info(f"ocr_result: {ocr_result}")
                if confidence < 0.9:
                    # 현재 메시지의 content에서 언어 감지
                    is_korean = False
                    if message_content:
                        for content in message_content:
                            if isinstance(content.get("text"), str):
                                # 한글 문자가 포함되어 있는지 확인
                                if any('\uAC00' <= char <= '\uD7A3' for char in content.get("text", "")):
                                    is_korean = True
                                    break
                    
                    error_message = "I'm sorry, but as a text-based AI model, I'm unable to view or analyze images directly. If you need help with the image, please describe its contents in text or ask specific questions about it, and I'll do my best to assist you."
                    if is_korean:
                        error_message = "죄송합니다만, 현재 저는 텍스트 기반 AI 모델이라 이미지를 직접 확인하거나 분석할 수 없습니다. 이미지에 대해 도움이 필요하시다면, 이미지의 내용을 텍스트로 설명해 주시거나 구체적인 질문을 해주시면 최대한 도움을 드리겠습니다."
                    
                    return {
                        "type": "image_ocr_error",
                        "text": error_message,
                        "confidence": confidence
                    }
                else:
                    # 전체 텍스트
                    extracted_text = ocr_result.get("text", "")

                    # 단어 + 위치 정보
                    words_info = []
                    for page in ocr_result.get("pages", []):
                        for word in page.get("words", []):
                            words_info.append({
                                "text": word.get("text", ""),
                                "boundingBox": word.get("boundingBox", {}),
                                "confidence": word.get("confidence", 0)
                            })

                    return {
                        "type": "text",
                        "text": extracted_text,
                        "words": words_info,
                        "confidence": confidence
                    }
        else:
            # base64가 아니면 패스하거나 에러처리
            return {
                "type": "text",
                "text": "(Invalid image data)",
                "confidence": 0
            }
    except Exception as e:
        # log.exception(e)
        raise e

@router.post("/chat/completions")
async def generate_chat_completion(
    request: Request,
    form_data: dict,
    user=Depends(get_verified_user),
    bypass_filter: Optional[bool] = False,
):
    print("hello!!!")
    if BYPASS_MODEL_ACCESS_CONTROL:
        bypass_filter = True

    idx = 0

    payload = {**form_data}
    metadata = payload.pop("metadata", None)

    model_id = form_data.get("model")
    model_info = Models.get_model_by_id(model_id)

    # Check model info and override the payload
    if model_info:
        if model_info.base_model_id:
            payload["model"] = model_info.base_model_id
            model_id = model_info.base_model_id

        params = model_info.params.model_dump()
        payload = apply_model_params_to_body_openai(params, payload)
        payload = apply_model_system_prompt_to_body(params, payload, metadata, user)

        # Check if user has access to the model
        if not bypass_filter and user.role == "user":
            if not (
                user.id == model_info.user_id
                or has_access(
                    user.id, type="read", access_control=model_info.access_control
                )
            ):
                raise HTTPException(
                    status_code=403,
                    detail="Model not found",
                )
    elif not bypass_filter:
        if user.role != "admin":
            raise HTTPException(
                status_code=403,
                detail="Model not found",
            )

    await get_all_models(request, user=user)
    # print(request.app.state.UPSTAGE_MODELS)
    model = request.app.state.UPSTAGE_MODELS.get(model_id)
    if model:
        idx = model["urlIdx"]
    else:
        raise HTTPException(
            status_code=404,
            detail="Model not found",
        )

    # Get the API config for the model
    api_config = request.app.state.config.UPSTAGE_API_CONFIGS.get(
        str(idx),
        request.app.state.config.UPSTAGE_API_CONFIGS.get(
            # request.app.state.config.UPSTAGE_API_BASE_URLS[idx], {}
            "https://api.upstage.ai/v1", {}
        ),  # Legacy support
    )

    prefix_id = api_config.get("prefix_id", None)
    if prefix_id:
        payload["model"] = payload["model"].replace(f"{prefix_id}.", "")

    # Add user info to the payload if the model is a pipeline
    if "pipeline" in model and model.get("pipeline"):
        payload["user"] = {
            "name": user.name,
            "id": user.id,
            "email": user.email,
            "role": user.role,
        }

    url = request.app.state.config.UPSTAGE_API_BASE_URLS[idx]
    key = request.app.state.config.UPSTAGE_API_KEYS[idx]

    # Fix: o1,o3 does not support the "max_tokens" parameter, Modify "max_tokens" to "max_completion_tokens"
    # is_o1_o3 = payload["model"].lower().startswith(("o1", "o3-"))
    # if is_o1_o3:
    #     payload = openai_o1_o3_handler(payload)
    if "api.upstage.ai" not in url:
        # Remove "max_completion_tokens" from the payload for backward compatibility
        if "max_completion_tokens" in payload:
            payload["max_tokens"] = payload["max_completion_tokens"]
            del payload["max_completion_tokens"]

    if "max_tokens" in payload and "max_completion_tokens" in payload:
        del payload["max_tokens"]

    # Convert the modified body back to JSON
    if "logit_bias" in payload:
        payload["logit_bias"] = json.loads(
            convert_logit_bias_input_to_json(payload["logit_bias"])
        )

    features = metadata.get("features", {})
    if features:
        if features.get("reasoning_effort", False) == True:
            if payload["model"] == "solar-pro2":
                payload["reasoning_effort"] = "high"

    # Convert image_url to text using ocr
    try:
        for message in payload["messages"]:
            if not isinstance(message["content"], str):
                for msg_part in message["content"]:
                    if msg_part.get("type") == "image_ocr_error":
                        error_message = msg_part.get("text", "Image processing failed.")
                        if form_data.get("stream", False):
                            async def error_stream():
                                yield f"data: {json.dumps({'choices': [{'delta': {'content': error_message}}]})}\n\n"
                                yield "data: [DONE]\n\n"
                            return StreamingResponse(error_stream(), media_type="text/event-stream")
                        else:
                            return {"choices": [{"message": {"content": error_message}}]}
            # image_url이 text로 변환된 경우는 그대로 content에 포함

    except Exception as e:
        # OCR 처리 루프 또는 그 외 로직에서 발생한 예외 처리
        log.exception(f"Error during message processing in generate_chat_completion: {e}")
        generic_error_message = "An error occurred while processing your request."
        if form_data.get("stream", False):
            async def error_stream():
                yield f"data: {json.dumps({'choices': [{'delta': {'content': generic_error_message}}]})}\\n\\n"
                yield "data: [DONE]\\n\\n"
            return StreamingResponse(error_stream(), media_type="text/event-stream")
        else:
            return {"choices": [{"message": {"content": generic_error_message}}]}

    # print("payload", payload)
    payload = json.dumps(payload)

    r = None
    session = None
    streaming = False
    response = None

    try:
        session = aiohttp.ClientSession(
            trust_env=True, timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
        )

        r = await session.request(
            method="POST",
            url=f"{url}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                **(
                    {
                        "HTTP-Referer": "https://openwebui.com/",
                        "X-Title": "Open WebUI",
                    }
                    if "openrouter.ai" in url
                    else {}
                ),
                **(
                    {
                        "X-OpenWebUI-User-Name": user.name,
                        "X-OpenWebUI-User-Id": user.id,
                        "X-OpenWebUI-User-Email": user.email,
                        "X-OpenWebUI-User-Role": user.role,
                    }
                    if ENABLE_FORWARD_USER_INFO_HEADERS
                    else {}
                ),
            },
        )

        # Check if response is SSE
        if "text/event-stream" in r.headers.get("Content-Type", ""):
            streaming = True
            return StreamingResponse(
                r.content,
                status_code=r.status,
                headers=dict(r.headers),
                background=BackgroundTask(
                    cleanup_response, response=r, session=session
                ),
            )
        else:
            try:
                response = await r.json()
            except Exception as e:
                log.error(e)
                response = await r.text()

            r.raise_for_status()
            return response
    except Exception as e:
        log.exception(e)

        detail = None
        if isinstance(response, dict):
            if "error" in response:
                detail = f"{response['error']['message'] if 'message' in response['error'] else response['error']}"
        elif isinstance(response, str):
            detail = response

        raise HTTPException(
            status_code=r.status if r else 500,
            detail=detail if detail else "Open WebUI: Server Connection Error",
        )
    finally:
        if not streaming and session:
            if r:
                r.close()
            await session.close()


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(path: str, request: Request, user=Depends(get_verified_user)):
    """
    Deprecated: proxy all requests to OpenAI API
    """

    body = await request.body()

    idx = 0
    url = request.app.state.config.UPSTAGE_API_BASE_URLS[idx]
    key = request.app.state.config.UPSTAGE_API_KEYS[idx]

    r = None
    session = None
    streaming = False

    try:
        session = aiohttp.ClientSession(trust_env=True)
        r = await session.request(
            method=request.method,
            url=f"{url}/{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                **(
                    {
                        "X-OpenWebUI-User-Name": user.name,
                        "X-OpenWebUI-User-Id": user.id,
                        "X-OpenWebUI-User-Email": user.email,
                        "X-OpenWebUI-User-Role": user.role,
                    }
                    if ENABLE_FORWARD_USER_INFO_HEADERS
                    else {}
                ),
            },
        )
        r.raise_for_status()

        # Check if response is SSE
        if "text/event-stream" in r.headers.get("Content-Type", ""):
            streaming = True
            return StreamingResponse(
                r.content,
                status_code=r.status,
                headers=dict(r.headers),
                background=BackgroundTask(
                    cleanup_response, response=r, session=session
                ),
            )
        else:
            response_data = await r.json()
            return response_data

    except Exception as e:
        log.exception(e)

        detail = None
        if r is not None:
            try:
                res = await r.json()
                log.error(res)
                if "error" in res:
                    detail = f"External: {res['error']['message'] if 'message' in res['error'] else res['error']}"
            except Exception:
                detail = f"External: {e}"
        raise HTTPException(
            status_code=r.status if r else 500,
            detail=detail if detail else "Open WebUI: Server Connection Error",
        )
    finally:
        if not streaming and session:
            if r:
                r.close()
            await session.close()
