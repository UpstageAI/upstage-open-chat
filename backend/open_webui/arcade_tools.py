"""Arcade Tools Registry"""

import json
import logging
import time
from typing import Optional
from uuid import uuid4
from fastapi import Request
import asyncio
import concurrent.futures


log = logging.getLogger(__name__)

ARCADE_TOOLS_TO_DISPLAY = {
    "Google Calendar": [
        "Google.ListCalendars",
        "Google.CreateEvent",
        "Google.ListEvents",
        "Google.UpdateEvent",
        "Google.DeleteEvent",
        "Google.FindTimeSlotsWhenEveryoneIsFree",
    ],
    "Google Contacts": [
        "Google.SearchContactsByEmail",
        "Google.SearchContactsByName",
        "Google.CreateContact",
    ],
    "Google Docs": [
        "Google.GetDocumentById",
        "Google.InsertTextAtEndOfDocument",
        "Google.CreateBlankDocument",
        "Google.CreateDocumentFromText",
    ],
    "Google Gmail": [
        "Google.SendEmail",
        "Google.SendDraftEmail",
        "Google.WriteDraftEmail",
        "Google.UpdateDraftEmail",
        "Google.DeleteDraftEmail",
        "Google.TrashEmail",
        "Google.ListDraftEmails",
        "Google.ListEmailsByHeader",
        "Google.ListEmails",
        "Google.SearchThreads",
        "Google.ListThreads",
        "Google.GetThread",
    ],
    # "Web": [
    #     "Web.ScrapeUrl",
    #     "Web.CrawlWebsite",
    #     "Web.GetCrawlStatus",
    #     "Web.GetCrawlData",
    #     "Web.CancelCrawl",
    #     "Web.MapWebsite",
    # ],
    "Outlook Calendar": [
        "Microsoft.CreateEvent",
        "Microsoft.GetEvent",
        "Microsoft.ListEventsInTimeRange",
    ],
    "Outlook Mail": [
        "Microsoft.CreateDraftEmail",
        "Microsoft.UpdateDraftEmail",
        "Microsoft.SendDraftEmail",
        "Microsoft.CreateAndSendEmail",
        "Microsoft.ReplyToEmail",
        "Microsoft.ListEmails",
        "Microsoft.ListEmailsInFolder",
    ],
    "Notion": [
        "NotionToolkit.GetPageContentById",
        "NotionToolkit.GetPageContentByTitle",
        "NotionToolkit.CreatePage",
        "NotionToolkit.SearchByTitle",
        "NotionToolkit.GetObjectMetadata",
        "NotionToolkit.GetWorkspaceStructure",
    ],
    "Youtube": [
        "Search.SearchYoutubeVideos",
        "Search.GetYoutubeVideoDetails",
    ],
    "Slack": [
        "Slack.SendDmToUser",
        "Slack.SendMessageToChannel",
        "Slack.GetMembersInConversationById",
        "Slack.GetMembersInChannelByName",
        "Slack.GetMessagesInConversationById",
        "Slack.GetMessagesInChannelByName",
        "Slack.GetMessagesInDirectMessageConversationByUsername",
        "Slack.GetMessagesInMultiPersonDmConversationByUsernames",
        "Slack.GetConversationMetadataById",
        "Slack.GetChannelMetadataByName",
        "Slack.GetDirectMessageConversationMetadataByUsername",
        "Slack.ListConversationsMetadata",
        "Slack.ListPublicChannelsMetadata",
        "Slack.ListPrivateChannelsMetadata",
        "Slack.ListGroupDirectMessageConversationsMetadata",
        "Slack.ListDirectMessageConversationsMetadata",
        "Slack.GetUserInfoById",
        "Slack.ListUsers",
    ],
} 

# new
async def chat_completion_arcade_tools_handler(
    request, body: dict, extra_params: dict, user, models, arcade_tools
) -> tuple[dict, dict]:
    # Import here to avoid circular imports
    from fastapi import Request
    from open_webui.constants import TASKS
    from open_webui.utils.task import get_task_model_id
    from open_webui.utils.misc import get_last_user_message
    from open_webui.utils.chat import generate_chat_completion
    from open_webui.utils.task import tools_function_calling_generation_template
    from open_webui.config import DEFAULT_TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE
    
    try:
        from arcadepy import Arcade
        client = Arcade(api_key=request.app.state.config.ARCADE_API_KEY)
    except ImportError:
        log.error("arcadepy package is not installed. Please install it to use Arcade tools.")
        return body, {}

    event_emitter = extra_params["__event_emitter__"]

    async def get_content_from_response(response) -> Optional[str]:
        content = None
        if hasattr(response, "body_iterator"):
            async for chunk in response.body_iterator:
                data = json.loads(chunk.decode("utf-8"))
                content = data["choices"][0]["message"]["content"]

            # Cleanup any remaining background tasks if necessary
            if response.background is not None:
                await response.background()
        else:
            content = response["choices"][0]["message"]["content"]
        return content

    def get_tools_function_calling_payload(messages, task_model_id, content):
        user_message = get_last_user_message(messages)
        history = "\n".join(
            f"{message['role'].upper()}: \"\"\"{message['content']}\"\"\""
            for message in messages[::-1][:4]
        )

        prompt = f"History:\n{history}\nQuery: {user_message}"

        return {
            "model": task_model_id,
            "messages": [
                {"role": "system", "content": content},
                {"role": "user", "content": f"Query: {prompt}"},
            ],
            "stream": False,
            "metadata": {"task": str(TASKS.FUNCTION_CALLING)},
        }

    event_caller = extra_params["__event_call__"]
    metadata = extra_params["__metadata__"]

    task_model_id = get_task_model_id( # ? Is it using solar mini model? then context window might be a problem
        body["model"],
        request.app.state.config.TASK_MODEL,
        request.app.state.config.TASK_MODEL_EXTERNAL,
        models,
    )

    skip_files = False
    sources = []

    # tools = request.app.state.ARCADE_TOOLS
    tools = arcade_tools

    specs = [
        {
            'description': tool.description,
            'name': tool.qualified_name,
            'parameters': {
                'properties': {
                    key.name: {'description': key.description,
                        'title': key.name,
                        'type': key.value_schema.val_type}
                    for key in tool.input.parameters
                },
                'required': [key.name for key in tool.input.parameters if key.required],
                'type': 'object'
            },
            'type': 'function'
        }
        for tool in tools
    ]
    

    def arcade_tool_callable(tool_name, user_id, event_emitter):
        async def _callable(**input_data):
            log.debug(f"{input_data=}")
            
            # Auto-set end time for calendar events if only start time is provided
            if "CreateEvent" in tool_name:
                try:
                    from datetime import datetime, timedelta
                    import re
                    
                    # Check for various start time field names and set corresponding end time
                    start_field = None
                    end_field = None
                    start_time_str = None
                    
                    # Handle Google Calendar API format: start/end objects with dateTime
                    if "start" in input_data and "end" not in input_data:
                        if isinstance(input_data["start"], dict) and "dateTime" in input_data["start"]:
                            start_time_str = input_data["start"]["dateTime"]
                            start_field = "start"
                            end_field = "end"
                    
                    # Handle other possible field name combinations
                    if not start_field:
                        time_field_pairs = [
                            ("start_datetime", "end_datetime"),
                            ("start_time", "end_time"),
                            ("startDateTime", "endDateTime"),
                            ("startTime", "endTime"),
                        ]
                        
                        for start_f, end_f in time_field_pairs:
                            if start_f in input_data and end_f not in input_data:
                                start_field = start_f
                                end_field = end_f
                                start_time_str = input_data[start_f]
                                break
                    
                    if start_field and start_time_str:
                        # Parse various datetime formats
                        datetime_formats = [
                            "%Y-%m-%dT%H:%M:%S%z",      # with timezone
                            "%Y-%m-%dT%H:%M:%S+09:00",  # with +09:00
                            "%Y-%m-%dT%H:%M:%S",
                            "%Y-%m-%d %H:%M:%S",
                            "%Y-%m-%dT%H:%M",
                            "%Y-%m-%d %H:%M",
                            "%Y-%m-%dT%H:%M:%SZ",
                            "%Y-%m-%dT%H:%M:%S.%fZ",
                        ]
                        
                        start_datetime = None
                        original_tz_info = ""
                        
                        # Extract timezone info if present
                        if "+09:00" in start_time_str:
                            original_tz_info = "+09:00"
                            start_time_clean = start_time_str.replace("+09:00", "")
                        elif "Z" in start_time_str:
                            original_tz_info = "Z"
                            start_time_clean = start_time_str.replace("Z", "")
                        else:
                            start_time_clean = start_time_str
                        
                        for fmt in datetime_formats:
                            try:
                                if "%z" in fmt:
                                    start_datetime = datetime.strptime(start_time_str, fmt)
                                else:
                                    start_datetime = datetime.strptime(start_time_clean, fmt)
                                break
                            except ValueError:
                                continue
                        
                        if start_datetime:
                            # Add 1 hour to start time
                            end_datetime = start_datetime + timedelta(hours=1)
                            
                            # Format end time
                            if original_tz_info:
                                end_time_formatted = end_datetime.strftime("%Y-%m-%dT%H:%M:%S") + original_tz_info
                            elif "T" in start_time_str:
                                end_time_formatted = end_datetime.strftime("%Y-%m-%dT%H:%M:%S")
                            else:
                                end_time_formatted = end_datetime.strftime("%Y-%m-%d %H:%M:%S")
                            
                            # Set the end time based on the field structure
                            if start_field == "start" and isinstance(input_data["start"], dict):
                                # Google Calendar API format
                                input_data["end"] = {"dateTime": end_time_formatted}
                            else:
                                # Simple field format
                                input_data[end_field] = end_time_formatted
                            
                            log.info(f"Auto-set {end_field} for {tool_name}: {end_time_formatted}")
                except Exception as e:
                    log.warning(f"Failed to auto-set end time for {tool_name}: {e}")
            
            auth_response = client.tools.authorize(
                tool_name=tool_name,
                user_id=user_id,
            )
            if auth_response.status != "completed":
                await event_emitter(
                    {
                        "type": "status",
                        "data": {
                            "action": "arcade_tool",
                            "description": f"Need to authorize tool {tool_name}",
                            "auth_url": auth_response.url,
                            "done": True,
                        },
                    }
                )
                return {
                    "result": None,
                    "status": "pending",
                    # "auth_url": auth_response.url,
                    "description": f"Need to get authorization from user with authentication url."
                                    "Inform user to click on auth button which would be provided near your response",
                }
            
            log.debug(f"{user_id=}")

            await event_emitter(
                {
                    "type": "status",
                    "data": {"action": "arcade_tool", "description": f"Executing tool {tool_name}", "done": False},
                }
            )

            try:

                start_time = time.time()

                response = client.tools.execute(
                    tool_name=tool_name,
                    input=input_data,
                    user_id=user_id,
                )

                await event_emitter(
                    {
                        "type": "status",
                        "data": {
                            "action": "arcade_tool",
                            "description": f"Tool {tool_name} executed with status {response.status} for {int(time.time() - start_time)} seconds",
                            "done": True,
                        },
                    }
                )
                return {
                    "result": response.output.value,
                    "status": response.status,
                    "description": f"Tool {tool_name} executed with status {response.status}",
                }
            except Exception as e:
                log.exception(e)

                await event_emitter(
                    {
                        "type": "status",
                        "data": {"action": "arcade_tool", "description": f"Error executing tool {tool_name}", "done": True},
                    }
                )

                return {
                    "result": None,
                    "status": "error",
                    "description": f"Error executing tool {tool_name} with error {e}",
                }
        
        return _callable

    async def list_tools_callable(**input_data):
        tools_list = [
            {
                "name": tool.qualified_name,
                "description": tool.description
            }
            for tool in arcade_tools
        ]

        await event_emitter(
            {
                "type": "status",
                "data": {
                    "action": "arcade_tool",
                    "description": f"List of all Arcade tools",
                    "done": True,
                },
            }
        )

        return {
            "result": tools_list,
            "status": "completed",
            "description": "List of all tools",
        }

    tools = {
        spec['name'] : {
            'spec': spec,
            "callable": arcade_tool_callable(spec['name'], user.id, event_emitter)
        }
        for spec in specs
    }

    # Add list_tools to the specs
    list_tools_spec = {
        'description': "List of all tools",
        'name': "list_tools",
        'parameters': {
            'properties':{},
            'required': [],
            'type': 'object'
        },
        'type': 'function'
    }
    specs.append(list_tools_spec)

    tools_specs = json.dumps(specs)

    tools["list_tools"] = {
        'spec': list_tools_spec,
        'callable': list_tools_callable
    }

    if request.app.state.config.TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE != "":
        template = request.app.state.config.TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE
    else:
        template = DEFAULT_TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE

    tools_function_calling_prompt = tools_function_calling_generation_template(
        template, tools_specs
    )
    payload = get_tools_function_calling_payload(
        body["messages"], task_model_id, tools_function_calling_prompt
    )
    # print("tools_function_calling_prompt", tools_function_calling_prompt)

    try:
        await event_emitter(
            {
                "type": "status",
                "data": {"action": "arcade_tool", "description": "Selecting right tool", "done": False},
            }
        )
        response = await generate_chat_completion(request, form_data=payload, user=user)
        log.debug(f"{response=}")
        content = await get_content_from_response(response)
        log.debug(f"{content=}")

        if not content:
            await event_emitter(
                {
                    "type": "status",
                    "data": {"action": "arcade_tool", "description": "No response from tool", "done": True},
                }
            )
            return body, {}

        try:
            content = content[content.find("{") : content.rfind("}") + 1]
            if not content:
                raise Exception("No JSON object found in the response")

            result = json.loads(content)

            async def tool_call_handler(tool_call):
                nonlocal skip_files

                log.debug(f"{tool_call=}")

                tool_function_name = tool_call.get("name", None)

                if tool_function_name not in tools:
                    await event_emitter(
                        {
                            "type": "status",
                            "data": {"action": "arcade_tool", "description": "No response from tool", "done": True},
                        }
                    )
                    return body, {}

                tool_function_params = tool_call.get("parameters", {})

                try:
                    tool = tools[tool_function_name]

                    spec = tool.get("spec", {})
                    allowed_params = (
                        spec.get("parameters", {}).get("properties", {}).keys()
                    )
                    tool_function_params = {
                        k: v
                        for k, v in tool_function_params.items()
                        if k in allowed_params
                    }

                    if tool.get("direct", False):
                        tool_result = await event_caller(
                            {
                                "type": "execute:tool",
                                "data": {
                                    "id": str(uuid4()),
                                    "name": tool_function_name,
                                    "params": tool_function_params,
                                    "server": tool.get("server", {}),
                                    "session_id": metadata.get("session_id", None),
                                },
                            }
                        )
                    else:
                        tool_function = tool["callable"]
                        tool_result = await tool_function(**tool_function_params)

                        # if tool_result.get("status") == "pending":
                        #     return {
                        #         "result": None,
                        #         "status": "pending",
                        #         "auth_url": tool_result.get("auth_url"),
                        #     }
                        
                except Exception as e:
                    tool_result = str(e)

                tool_result_files = []
                if isinstance(tool_result, list):
                    for item in tool_result:
                        # check if string
                        if isinstance(item, str) and item.startswith("data:"):
                            tool_result_files.append(item)
                            tool_result.remove(item)

                if isinstance(tool_result, dict) or isinstance(tool_result, list):
                    tool_result = json.dumps(tool_result, indent=2, ensure_ascii=False)

                if isinstance(tool_result, str):
                    tool = tools[tool_function_name]
                    tool_id = tool.get("tool_id", "")
                    if tool.get("metadata", {}).get("citation", False) or tool.get(
                        "direct", False
                    ):

                        sources.append(
                            {
                                "source": {
                                    "name": (
                                        f"TOOL:" + f"{tool_id}/{tool_function_name}"
                                        if tool_id
                                        else f"{tool_function_name}"
                                    ),
                                },
                                "document": [tool_result, *tool_result_files],
                                "metadata": [
                                    {
                                        "source": (
                                            f"TOOL:" + f"{tool_id}/{tool_function_name}"
                                            if tool_id
                                            else f"{tool_function_name}"
                                        )
                                    }
                                ],
                            }
                        )
                    else:
                        sources.append(
                            {
                                "source": {},
                                "document": [tool_result, *tool_result_files],
                                "metadata": [
                                    {
                                        "source": (
                                            f"TOOL:" + f"{tool_id}/{tool_function_name}"
                                            if tool_id
                                            else f"{tool_function_name}"
                                        )
                                    }
                                ],
                            }
                        )

                    if (
                        tools[tool_function_name]
                        .get("metadata", {})
                        .get("file_handler", False)
                    ):
                        skip_files = True

            # check if "tool_calls" in result
            if result.get("tool_calls"):
                for tool_call in result.get("tool_calls"):
                    await tool_call_handler(tool_call)
            else:
                await tool_call_handler(result)

        except Exception as e:
            log.debug(f"Error: {e}")
            content = None
    except Exception as e:
        await event_emitter(
            {
                "type": "status",
                "data": {"action": "arcade_tool", "description": "No response from tool", "done": True},
            }
        )
        log.debug(f"Error: {e}")
        content = None

    log.debug(f"tool_contexts: {sources}")

    if skip_files and "files" in body.get("metadata", {}):
        del body["metadata"]["files"]

    return body, {"sources": sources}


async def get_arcade_tools(request: Request, user) -> list["ToolUserResponse"]:
    """Extract and process arcade tools from the application state."""
    print("[DEBUG] get_arcade_tools called")
    
    # Import here to avoid circular imports
    from open_webui.models.tools import ToolUserResponse
    
    from arcadepy import Arcade
    client = Arcade()

    arcade_tools = []
    arcade_tool_mapper = {}
    
    # Check if arcade tools are properly initialized
    if not hasattr(request.app.state, 'ARCADE_TOOLS'):
        print("[DEBUG] ARCADE_TOOLS not found in app state")
        return arcade_tools
        
    if not hasattr(request.app.state.config, 'ARCADE_TOOLS_CONFIG'):
        print("[DEBUG] ARCADE_TOOLS_CONFIG not found in config")
        return arcade_tools
    
    print(f"[DEBUG] Found {len(request.app.state.ARCADE_TOOLS)} arcade tools, {len(request.app.state.config.ARCADE_TOOLS_CONFIG)} tool configs")
    
    for idx, tool in enumerate(request.app.state.ARCADE_TOOLS):
        arcade_tool_mapper[tool.qualified_name] = tool

    # Prepare auth tasks for parallel execution
    auth_tasks = []
    tool_kit_info = []

    for idx, tool_kit in enumerate(request.app.state.config.ARCADE_TOOLS_CONFIG):
        if tool_kit.get('enabled'):
            print(f"[DEBUG] Processing enabled tool_kit: {tool_kit.get('toolkit')}")
            
            all_scopes = set()
            auth_id = None
            auth_provider_id = None 
            auth_provider_type = None
            
            for tool in tool_kit.get('tools', []):
                tool_name = tool.get('name')
                
                if tool_name in arcade_tool_mapper:
                    arcade_tool = arcade_tool_mapper[tool_name]
                    requirements = arcade_tool.requirements
                    
                    if requirements and requirements.authorization:
                        auth = requirements.authorization
                        if auth.oauth2 and auth.oauth2.scopes:
                            all_scopes.update(auth.oauth2.scopes)
                        # Use the first non-None values we find
                        auth_id = auth_id or auth.id
                        auth_provider_id = auth_provider_id or auth.provider_id
                        auth_provider_type = auth_provider_type or auth.provider_type
            
            if auth_provider_id and auth_provider_type:
                auth_requirement = {
                    "provider_id": auth_provider_id,
                    "provider_type": auth_provider_type,
                    "oauth2": {
                        "scopes": list(all_scopes)
                    }
                }
                if auth_id:
                    auth_requirement["id"] = auth_id
                else:
                    auth_requirement["id"] = None
                log.info(f"{auth_requirement=}")
                
                # Create auth task for async execution
                auth_task = asyncio.create_task(
                    asyncio.to_thread(
                        client.auth.authorize,
                        auth_requirement=auth_requirement,
                        user_id=user.id
                    )
                )
                auth_tasks.append(auth_task)
                tool_kit_info.append((idx, tool_kit, len(auth_tasks) - 1))  # Store auth task index
            else:
                # No auth required
                tool_kit_info.append((idx, tool_kit, -1))  # -1 means no auth
    
    # Execute all auth requests in parallel
    auth_results = []
    if auth_tasks:
        auth_results = await asyncio.gather(*auth_tasks, return_exceptions=True)
    
    # Process results
    for idx, tool_kit, auth_task_idx in tool_kit_info:
        auth_result = None
        if auth_task_idx >= 0 and auth_task_idx < len(auth_results):
            auth_result = auth_results[auth_task_idx]
            if isinstance(auth_result, Exception):
                print(f"[DEBUG] Auth failed for {tool_kit.get('toolkit')}: {auth_result}")
                auth_result = None
        
        # Add tool regardless of auth result
        arcade_tools.append(
            ToolUserResponse(
                **{
                    "id": f"arcade:{idx}",
                    "user_id": f"arcade:{idx}",
                    "name": tool_kit.get('toolkit'),
                    "meta": {
                        "description": tool_kit.get('description'),
                        "auth_completed": True if auth_result and auth_result.status == "completed" else (True if not auth_result else False),
                        "auth_url": auth_result.url if auth_result else None,
                    },
                    "access_control": None,
                    "updated_at": int(time.time()),
                    "created_at": int(time.time()),
                }   
            )
        )
        print(f"[DEBUG] Added tool: {tool_kit.get('toolkit')}")
    
    return arcade_tools