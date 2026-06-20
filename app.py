from __future__ import annotations

import sys
import platform

# Mock platform / sys windows version calls to bypass Win32 API hangs caused by security software/Defender
if sys.platform == "win32":
    try:
        from collections import namedtuple
        uname_result = namedtuple("uname_result", ["system", "node", "release", "version", "machine", "processor"])
        platform.uname = lambda: uname_result("Windows", "localhost", "10", "10.0.19045", "AMD64", "Intel64 Family")
        platform.system = lambda: "Windows"
        
        class MockWindowsVersion:
            major = 10
            minor = 0
            build = 22621
            platform = 2
            service_pack = ""
            def __getitem__(self, item):
                return (10, 0, 22621, 2, "")[item]
        sys.getwindowsversion = lambda: MockWindowsVersion()
    except Exception:
        pass

import asyncio
import json
import logging
import os
import uuid
from typing import Dict, List, Optional, Union, Any
from datetime import datetime, UTC
from enum import Enum

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.encoders import jsonable_encoder
from langchain_mistralai import ChatMistralAI
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_ollama import ChatOllama
from langchain_openai import AzureChatOpenAI

# browser-use native LLM classes (implement the required BaseChatModel protocol)
from browser_use.llm.anthropic.chat import ChatAnthropic as BUChatAnthropic
from browser_use.llm.views import ChatInvokeUsage, ChatInvokeCompletion
from browser_use.llm.openai.chat import ChatOpenAI as BUChatOpenAI
from browser_use.llm.google.chat import ChatGoogle as BUChatGoogle
from browser_use.llm.mistral.chat import ChatMistral as BUChatMistral
from pydantic import BaseModel, Field

class PatchedChatAnthropic(BUChatAnthropic):
    def _get_usage(self, response: Any) -> Any:
        try:
            input_tokens = getattr(response.usage, 'input_tokens', 0) or 0
            output_tokens = getattr(response.usage, 'output_tokens', 0) or 0
            cache_read = getattr(response.usage, 'cache_read_input_tokens', 0) or 0
            cache_creation = getattr(response.usage, 'cache_creation_input_tokens', 0) or 0
            
            cache_creation_5m_tokens, cache_creation_1h_tokens = self._get_cache_creation_tokens(response)
            
            return ChatInvokeUsage(
                prompt_tokens=input_tokens + cache_read,
                completion_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                prompt_cached_tokens=cache_read,
                prompt_cache_creation_tokens=cache_creation,
                prompt_cache_creation_5m_tokens=cache_creation_5m_tokens,
                prompt_cache_creation_1h_tokens=cache_creation_1h_tokens,
                prompt_image_tokens=None,
                pricing_multiplier=self._get_pricing_multiplier(),
            )
        except Exception:
            return None

class PatchedChatOpenAI(BUChatOpenAI):
    async def ainvoke(self, messages: list[Any], output_format: type[T] | None = None, **kwargs: Any) -> Any:
        try:
            return await super().ainvoke(messages, output_format, **kwargs)
        except Exception as e:
            if output_format is not None:
                try:
                    logger.info("Structured output validation failed, attempting to manually parse raw completion...")
                    raw_completion = await super().ainvoke(messages, output_format=None, **kwargs)
                    text = raw_completion.completion.strip()
                    
                    if text.startswith("```"):
                        lines = text.splitlines()
                        if len(lines) >= 3:
                            text = "\n".join(lines[1:-1]).strip()
                            
                    if text.startswith("```json"):
                        text = text[7:]
                    if text.endswith("```"):
                        text = text[:-3]
                    text = text.strip()
                    
                    parsed = output_format.model_validate_json(text)
                    return ChatInvokeCompletion(
                        completion=parsed,
                        usage=raw_completion.usage,
                        stop_reason=raw_completion.stop_reason,
                    )
                except Exception as inner_e:
                    logger.error(f"Manual parsing fallback also failed: {inner_e}")
                    raise e
            raise

# This import will work once browser-use is installed
# For development, you may need to add the browser-use repo to your PYTHONPATH
from browser_use import Agent
from browser_use.agent.views import AgentHistoryList
from browser_use.browser.session import BrowserSession
from douyin_scraper import (
    scrape_douyin_channel,
    ScrapeChannelRequest,
    ScrapeChannelResponse,
    get_channel_video_list,
    get_video_detail,
    run_phase2_background,
    ChannelVideoListRequest,
    ChannelVideoListResponse,
    VideoDetailRequest,
    VideoDetailResponse,
)

# Define task status enum
class TaskStatus(str, Enum):
    CREATED = "created"  # Task is initialized but not yet started
    RUNNING = "running"  # Task is currently executing
    FINISHED = "finished"  # Task has completed successfully
    STOPPED = "stopped"  # Task was manually stopped
    PAUSED = "paused"  # Task execution is temporarily paused
    FAILED = "failed"  # Task encountered an error and could not complete
    STOPPING = "stopping"  # Task is in the process of stopping (transitional state)

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("browser-use-bridge")

app = FastAPI(title="Browser Use Bridge API")

# Custom JSON encoder for Enum serialization
class EnumJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Enum):
            return obj.value
        return super().default(obj)

# Configure FastAPI to use custom JSON serialization for responses
@app.middleware("http")
async def add_json_serialization(request: Request, call_next):
    response = await call_next(request)
    
    # Only attempt to modify JSON responses and check if body() method exists
    if response.headers.get("content-type") == "application/json" and hasattr(response, "body"):
        try:
            content = await response.body()
            content_str = content.decode("utf-8")
            content_dict = json.loads(content_str)
            # Convert any Enum values to their string representation
            content_str = json.dumps(content_dict, cls=EnumJSONEncoder)
            response = Response(
                content=content_str, 
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type="application/json"
            )
        except Exception as e:
            logger.error(f"Error serializing JSON response: {str(e)}")
    
    return response

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Task storage - in memory for now
tasks: Dict[str, Dict] = {}

# Models
class TaskRequest(BaseModel):
    task: str
    ai_provider: Optional[str] = "openai"  # Default to OpenAI
    save_browser_data: Optional[bool] = False  # Whether to save browser cookies
    headful: Optional[bool] = None  # Override BROWSER_USE_HEADFUL setting
    use_custom_chrome: Optional[bool] = None  # Whether to use custom Chrome from env vars

class TaskResponse(BaseModel):
    id: str
    status: str
    live_url: str
    
class TaskStatusResponse(BaseModel):
    status: str
    result: Optional[str] = None
    error: Optional[str] = None

# Utility functions
def get_llm(ai_provider: str):
    """Get LLM based on provider.
    
    Uses browser-use native LLM classes which implement the required BaseChatModel
    protocol (including the 'provider' property).
    
    If ai_provider is 'anthropic' and MEIAI_AUTH_TOKEN + MEIAI_BASE_URL are set,
    the request is transparently routed to MeiAI (Anthropic-compatible proxy).
    """
    if ai_provider == "anthropic":
        base_url = os.environ.get("ANTHROPIC_BASE_URL") or os.environ.get("MEIAI_BASE_URL")
        auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("MEIAI_AUTH_TOKEN")
        model = os.environ.get("ANTHROPIC_MODEL") or os.environ.get("MEIAI_MODEL_ID") or os.environ.get("ANTHROPIC_MODEL_ID", "claude-3-opus-20240229")
        
        if base_url:
            if "9router" in base_url or model == "gemini":
                logger.info(f"Routing 'anthropic' provider to OpenAI-compatible client for proxy: {base_url}")
                openai_base_url = base_url
                if not openai_base_url.endswith("/v1") and not openai_base_url.endswith("/v1/"):
                    openai_base_url = openai_base_url.rstrip("/") + "/v1"
                return PatchedChatOpenAI(
                    model=model,
                    api_key=auth_token or os.environ.get("ANTHROPIC_API_KEY"),
                    base_url=openai_base_url,
                    dont_force_structured_output=True,
                    add_schema_to_system_prompt=True,
                )
                
            logger.info(f"Routing 'anthropic' provider to custom proxy: {base_url}")
            kwargs = {
                "model": model,
                "base_url": base_url,
            }
            if auth_token:
                kwargs["auth_token"] = auth_token
                kwargs["api_key"] = auth_token
            else:
                kwargs["api_key"] = os.environ.get("ANTHROPIC_API_KEY")
            return PatchedChatAnthropic(**kwargs)
            
        return PatchedChatAnthropic(
            model=model,
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
        )
    elif ai_provider == "meiai":
        base_url = os.environ.get("MEIAI_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL", "https://meiai.onrender.com")
        auth_token = os.environ.get("MEIAI_AUTH_TOKEN") or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
        model = os.environ.get("MEIAI_MODEL_ID") or os.environ.get("ANTHROPIC_MODEL", "deepseek-v4-flash")
        
        if "9router" in base_url or model == "gemini":
            openai_base_url = base_url
            if not openai_base_url.endswith("/v1") and not openai_base_url.endswith("/v1/"):
                openai_base_url = openai_base_url.rstrip("/") + "/v1"
            return PatchedChatOpenAI(
                model=model,
                api_key=auth_token,
                base_url=openai_base_url,
                dont_force_structured_output=True,
                add_schema_to_system_prompt=True,
            )
            
        kwargs = {
            "model": model,
            "base_url": base_url,
        }
        if auth_token:
            kwargs["auth_token"] = auth_token
            kwargs["api_key"] = auth_token
        return PatchedChatAnthropic(**kwargs)
    elif ai_provider == "openai":
        kwargs = {
            "model": os.environ.get("OPENAI_MODEL_ID", "gpt-4o"),
            "api_key": os.environ.get("OPENAI_API_KEY"),
        }
        base_url = os.environ.get("OPENAI_BASE_URL")
        if base_url:
            kwargs["base_url"] = base_url
            if "9router" in base_url or "gemini" in kwargs["model"]:
                kwargs["dont_force_structured_output"] = True
                kwargs["add_schema_to_system_prompt"] = True
        return PatchedChatOpenAI(**kwargs)
    elif ai_provider == "google":
        return BUChatGoogle(
            model=os.environ.get("GOOGLE_MODEL_ID", "gemini-1.5-pro"),
            api_key=os.environ.get("GOOGLE_API_KEY"),
        )
    elif ai_provider == "mistral":
        return BUChatMistral(
            model=os.environ.get("MISTRAL_MODEL_ID", "mistral-large-latest"),
            api_key=os.environ.get("MISTRAL_API_KEY"),
        )
    elif ai_provider == "ollama":
        # Ollama uses OpenAI-compatible API, route via BUChatOpenAI with local base_url
        return PatchedChatOpenAI(
            model=os.environ.get("OLLAMA_MODEL_ID", "llama3"),
            api_key="ollama",  # Ollama doesn't need a real key
            base_url=os.environ.get("OLLAMA_API_BASE", "http://localhost:11434") + "/v1",
        )
    elif ai_provider == "azure":
        from browser_use.llm.azure.chat import ChatAzureOpenAI as BUChatAzure
        return BUChatAzure(
            model=os.environ.get("AZURE_DEPLOYMENT_NAME", "gpt-4o"),
            api_key=os.environ.get("AZURE_API_KEY"),
            azure_endpoint=os.environ.get("AZURE_ENDPOINT"),
        )
    else:
        # Default to OpenAI
        kwargs = {
            "model": os.environ.get("OPENAI_MODEL_ID", "gpt-4o"),
            "api_key": os.environ.get("OPENAI_API_KEY"),
        }
        base_url = os.environ.get("OPENAI_BASE_URL")
        if base_url:
            kwargs["base_url"] = base_url
        return PatchedChatOpenAI(**kwargs)

async def execute_task(task_id: str, instruction: str, ai_provider: str):
    """Execute browser task in background
    
    Chrome paths (CHROME_PATH and CHROME_USER_DATA) are only sourced from 
    environment variables for security reasons.
    """
    # Initialize browser variable outside the try block
    browser = None
    try:
        # Update task status
        tasks[task_id]["status"] = TaskStatus.RUNNING
        
        # Get LLM
        llm = get_llm(ai_provider)
        
        # Get task-specific browser configuration if available
        task_browser_config = tasks[task_id].get("browser_config", {})
        
        # Configure browser headless/headful mode (task setting overrides env var)
        task_headful = task_browser_config.get("headful")
        if task_headful is not None:
            headful = task_headful
        else:
            headful = os.environ.get("BROWSER_USE_HEADFUL", "false").lower() == "true"
        
        # Get Chrome path and user data directory (task settings override env vars)
        use_custom_chrome = task_browser_config.get("use_custom_chrome")
        
        if use_custom_chrome is False:
            # Explicitly disabled custom Chrome for this task
            chrome_path = None
            chrome_user_data = None
        else:
            # Only use environment variables for Chrome paths
            chrome_path = os.environ.get("CHROME_PATH")
            chrome_user_data = os.environ.get("CHROME_USER_DATA")
        
        # Configure agent options - start with basic configuration
        agent_kwargs = {
            "task": instruction,
            "llm": llm,
        }
        
        # Only configure and include browser if we need a custom browser setup
        if not headful or chrome_path:
            # Configure BrowserSession directly (new browser-use API)
            session_kwargs = {
                "headless": not headful,
            }

            # Add Chrome executable path if provided
            if chrome_path:
                session_kwargs["executable_path"] = chrome_path
                logger.info(f"Task {task_id}: Using custom Chrome executable: {chrome_path}")

            # Add Chrome user data directory if provided
            if chrome_user_data:
                session_kwargs["user_data_dir"] = chrome_user_data
                logger.info(f"Task {task_id}: Using Chrome user data directory: {chrome_user_data}")

            logger.info(f"Task {task_id}: Browser session args: headless={session_kwargs.get('headless')}")

            browser = BrowserSession(**session_kwargs)

            # Add browser session to agent kwargs
            agent_kwargs["browser_session"] = browser
        
        logger.info(f"Agent kwargs: {agent_kwargs}")
        # Pass the browser to Agent
        agent = Agent(**agent_kwargs)
        
        # Store agent in tasks
        tasks[task_id]["agent"] = agent
        
        # Create a step tracking callback
        async def step_callback(step_data):
            step_id = str(uuid.uuid4())
            step_num = len(tasks[task_id]["steps"]) + 1
            
            step = {
                "id": step_id,
                "step": step_num,
                "evaluation_previous_goal": step_data.get("evaluation", ""),
                "next_goal": step_data.get("goal", "")
            }
            
            tasks[task_id]["steps"].append(step)
        
        # Add callback to agent if available in this version
        if hasattr(agent, "add_callback"):
            agent.add_callback("on_step", step_callback)
        
        # Run agent
        result = await agent.run()
        
        # Extract result FIRST before any cleanup
        if isinstance(result, AgentHistoryList):
            final_result = result.final_result()
            tasks[task_id]["output"] = final_result
        else:
            tasks[task_id]["output"] = str(result) if result else None

        # Only mark finished if we actually got a result
        if tasks[task_id]["output"] is not None:
            tasks[task_id]["status"] = TaskStatus.FINISHED
        else:
            # Agent ran but returned no result - treat as failure
            tasks[task_id]["status"] = TaskStatus.FAILED
            tasks[task_id]["error"] = "Agent completed but returned no output. Check browser config or task instructions."
            logger.warning(f"Task {task_id}: agent returned no output (steps: {len(tasks[task_id]['steps'])})")

        tasks[task_id]["finished_at"] = datetime.now(UTC).isoformat()
        
        # Collect browser data if requested (non-critical, errors won't affect task status)
        if tasks[task_id]["save_browser_data"] and hasattr(agent, "browser_session"):
            try:
                browser_session = agent.browser_session
                if hasattr(browser_session, "get_cookies"):
                    cookies = await browser_session.get_cookies()
                    tasks[task_id]["browser_data"] = {"cookies": cookies}
                elif hasattr(browser_session, "context") and hasattr(browser_session.context, "cookies"):
                    cookies = await browser_session.context.cookies()
                    tasks[task_id]["browser_data"] = {"cookies": cookies}
                else:
                    tasks[task_id]["browser_data"] = {"cookies": [], "error": "No method available to collect cookies"}
            except Exception as e:
                logger.warning(f"Could not collect browser data for task {task_id} (non-critical): {str(e)}")
                tasks[task_id]["browser_data"] = {"cookies": [], "error": str(e)}
                
    except Exception as e:
        logger.exception(f"Error executing task {task_id}")
        # Only mark as FAILED if not already finished successfully
        if tasks[task_id].get("status") != TaskStatus.FINISHED:
            tasks[task_id]["status"] = TaskStatus.FAILED
            tasks[task_id]["error"] = str(e)
            tasks[task_id]["finished_at"] = datetime.now(UTC).isoformat()
        else:
            # Task completed but had post-processing error (e.g. browser cleanup)
            logger.warning(f"Task {task_id} completed successfully but had cleanup error: {str(e)}")
    finally:
        # Always close the browser session, regardless of success or failure
        if browser is not None:
            logger.info(f"Closing browser session for task {task_id}")
            try:
                await browser.stop()
                logger.info(f"Browser session closed successfully for task {task_id}")
            except Exception as e:
                logger.error(f"Error closing browser session for task {task_id}: {str(e)}")

# API Routes
@app.post("/api/v1/run-task", response_model=TaskResponse)
async def run_task(request: TaskRequest):
    """Start a browser automation task"""
    task_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    
    # Initialize task record
    tasks[task_id] = {
        "id": task_id,
        "task": request.task,
        "ai_provider": request.ai_provider,
        "status": TaskStatus.CREATED,
        "created_at": now,
        "finished_at": None,
        "output": None,  # Final result
        "error": None,
        "steps": [],  # Will store step information
        "agent": None,
        "save_browser_data": request.save_browser_data,
        "browser_data": None,  # Will store browser cookies if requested
        # Store browser configuration options
        "browser_config": {
            "headful": request.headful,
            "use_custom_chrome": request.use_custom_chrome,
        }
    }
    
    # Generate live URL
    live_url = f"/live/{task_id}"
    tasks[task_id]["live_url"] = live_url
    
    # Start task in background
    asyncio.create_task(execute_task(task_id, request.task, request.ai_provider))
    
    return TaskResponse(
        id=task_id,
        status=TaskStatus.CREATED,
        live_url=live_url
    )

@app.get("/api/v1/task/{task_id}/status", response_model=TaskStatusResponse)
async def get_task_status(task_id: str):
    """Get status of a task"""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    
    return TaskStatusResponse(
        status=tasks[task_id]["status"],
        result=tasks[task_id].get("output"),
        error=tasks[task_id].get("error")
    )

@app.get("/api/v1/task/{task_id}", response_model=dict)
async def get_task(task_id: str):
    """Get full task details"""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    
    # Return task data excluding agent object
    task_data = {k: v for k, v in tasks[task_id].items() if k != "agent"}
    return task_data

@app.put("/api/v1/stop-task/{task_id}")
async def stop_task(task_id: str):
    """Stop a running task"""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    
    if tasks[task_id]["status"] in [TaskStatus.FINISHED, TaskStatus.FAILED, TaskStatus.STOPPED]:
        return {"message": f"Task already in terminal state: {tasks[task_id]['status']}"}
    
    # Get agent
    agent = tasks[task_id].get("agent")
    if agent:
        # Call agent's stop method
        agent.stop()
        tasks[task_id]["status"] = TaskStatus.STOPPING
        return {"message": "Task stopping"}
    else:
        tasks[task_id]["status"] = TaskStatus.STOPPED
        tasks[task_id]["finished_at"] = datetime.now(UTC).isoformat()
        return {"message": "Task stopped (no agent found)"}

@app.put("/api/v1/pause-task/{task_id}")
async def pause_task(task_id: str):
    """Pause a running task"""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    
    if tasks[task_id]["status"] != TaskStatus.RUNNING:
        return {"message": f"Task not running: {tasks[task_id]['status']}"}
    
    # Get agent
    agent = tasks[task_id].get("agent")
    if agent:
        # Call agent's pause method
        agent.pause()
        tasks[task_id]["status"] = TaskStatus.PAUSED
        return {"message": "Task paused"}
    else:
        return {"message": "Task could not be paused (no agent found)"}

@app.put("/api/v1/resume-task/{task_id}")
async def resume_task(task_id: str):
    """Resume a paused task"""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    
    if tasks[task_id]["status"] != TaskStatus.PAUSED:
        return {"message": f"Task not paused: {tasks[task_id]['status']}"}
    
    # Get agent
    agent = tasks[task_id].get("agent")
    if agent:
        # Call agent's resume method
        agent.resume()
        tasks[task_id]["status"] = TaskStatus.RUNNING
        return {"message": "Task resumed"}
    else:
        return {"message": "Task could not be resumed (no agent found)"}

@app.get("/api/v1/list-tasks")
async def list_tasks():
    """List all tasks"""
    task_list = []
    for task_id, task_data in tasks.items():
        # Return task data excluding agent object
        task_summary = {
            "id": task_data["id"],
            "status": task_data["status"],
            "task": task_data.get("task", ""),
            "created_at": task_data.get("created_at", ""),
            "finished_at": task_data.get("finished_at"),
            "live_url": task_data.get("live_url", f"/live/{task_id}")
        }
        task_list.append(task_summary)
    
    return {
        "tasks": task_list,
        "total": len(task_list),
        "page": 1,
        "per_page": 100
    }

@app.get("/live/{task_id}", response_class=HTMLResponse)
async def live_view(task_id: str):
    """Get a live view of a task that can be embedded in an iframe"""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Browser Use Task {task_id}</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; padding: 20px; }}
            .container {{ max-width: 1200px; margin: 0 auto; }}
            .status {{ padding: 10px; border-radius: 4px; margin-bottom: 20px; }}
            .{TaskStatus.RUNNING} {{ background-color: #e3f2fd; }}
            .{TaskStatus.FINISHED} {{ background-color: #e8f5e9; }}
            .{TaskStatus.FAILED} {{ background-color: #ffebee; }}
            .{TaskStatus.PAUSED} {{ background-color: #fff8e1; }}
            .{TaskStatus.STOPPED} {{ background-color: #eeeeee; }}
            .{TaskStatus.CREATED} {{ background-color: #f3e5f5; }}
            .{TaskStatus.STOPPING} {{ background-color: #fce4ec; }}
            .controls {{ margin-bottom: 20px; }}
            button {{ padding: 8px 16px; margin-right: 10px; cursor: pointer; }}
            pre {{ background-color: #f5f5f5; padding: 15px; border-radius: 4px; overflow: auto; }}
            .step {{ margin-bottom: 10px; padding: 10px; border: 1px solid #ddd; border-radius: 4px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Browser Use Task</h1>
            <div id="status" class="status">Loading...</div>
            
            <div class="controls">
                <button id="pauseBtn">Pause</button>
                <button id="resumeBtn">Resume</button>
                <button id="stopBtn">Stop</button>
            </div>
            
            <h2>Result</h2>
            <pre id="result">Loading...</pre>
            
            <h2>Steps</h2>
            <div id="steps">Loading...</div>
            
            <script>
                const taskId = '{task_id}';
                const FINISHED = '{TaskStatus.FINISHED}';
                const FAILED = '{TaskStatus.FAILED}';
                const STOPPED = '{TaskStatus.STOPPED}';
                
                // Update status function
                function updateStatus() {{
                    fetch(`/api/v1/task/${{taskId}}/status`)
                        .then(response => response.json())
                        .then(data => {{
                            // Update status element
                            const statusEl = document.getElementById('status');
                            statusEl.textContent = `Status: ${{data.status}}`;
                            statusEl.className = `status ${{data.status}}`;
                            
                            // Update result if available
                            if (data.result) {{
                                document.getElementById('result').textContent = data.result;
                            }} else if (data.error) {{
                                document.getElementById('result').textContent = `Error: ${{data.error}}`;
                            }}
                            
                            // Continue polling if not in terminal state
                            if (![FINISHED, FAILED, STOPPED].includes(data.status)) {{
                                setTimeout(updateStatus, 2000);
                            }}
                        }})
                        .catch(error => {{
                            console.error('Error fetching status:', error);
                            setTimeout(updateStatus, 5000);
                        }});
                        
                    // Also fetch full task to get steps
                    fetch(`/api/v1/task/${{taskId}}`)
                        .then(response => response.json())
                        .then(data => {{
                            if (data.steps && data.steps.length > 0) {{
                                const stepsHtml = data.steps.map(step => `
                                    <div class="step">
                                        <strong>Step ${{step.step}}</strong>
                                        <p>Next Goal: ${{step.next_goal || 'N/A'}}</p>
                                        <p>Evaluation: ${{step.evaluation_previous_goal || 'N/A'}}</p>
                                    </div>
                                `).join('');
                                document.getElementById('steps').innerHTML = stepsHtml;
                            }} else {{
                                document.getElementById('steps').textContent = 'No steps recorded yet.';
                            }}
                        }})
                        .catch(error => {{
                            console.error('Error fetching task details:', error);
                        }});
                }}
                
                // Setup control buttons
                document.getElementById('pauseBtn').addEventListener('click', () => {{
                    fetch(`/api/v1/pause-task/${{taskId}}`, {{ method: 'PUT' }})
                        .then(response => response.json())
                        .then(data => alert(data.message))
                        .catch(error => console.error('Error pausing task:', error));
                }});
                
                document.getElementById('resumeBtn').addEventListener('click', () => {{
                    fetch(`/api/v1/resume-task/${{taskId}}`, {{ method: 'PUT' }})
                        .then(response => response.json())
                        .then(data => alert(data.message))
                        .catch(error => console.error('Error resuming task:', error));
                }});
                
                document.getElementById('stopBtn').addEventListener('click', () => {{
                    if (confirm('Are you sure you want to stop this task? This action cannot be undone.')) {{
                        fetch(`/api/v1/stop-task/${{taskId}}`, {{ method: 'PUT' }})
                            .then(response => response.json())
                            .then(data => alert(data.message))
                            .catch(error => console.error('Error stopping task:', error));
                    }}
                }});
                
                // Start status updates
                updateStatus();
                
                // Refresh every 5 seconds
                setInterval(updateStatus, 5000);
            </script>
        </div>
    </body>
    </html>
    """
    
    return HTMLResponse(content=html_content)

@app.get("/api/v1/ping")
async def ping():
    """Health check endpoint"""
    return {"status": "success", "message": "API is running"}

@app.get("/api/v1/providers")
async def list_providers():
    """List available AI providers and their active configuration.
    
    When MeiAI is configured, the 'anthropic' provider is transparently routed to MeiAI.
    """
    meiai_token = os.environ.get("MEIAI_AUTH_TOKEN") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    meiai_base_url = os.environ.get("MEIAI_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL")
    meiai_active = bool(meiai_token and meiai_base_url)
    proxy_model = os.environ.get("ANTHROPIC_MODEL") or os.environ.get("MEIAI_MODEL_ID", "deepseek-v4-flash")

    return {
        "providers": [
            {
                "id": "anthropic",
                "label": "Anthropic (Claude)" if not meiai_active else f"Proxy via anthropic [{proxy_model}]",
                "active": meiai_active or bool(os.environ.get("ANTHROPIC_API_KEY")),
                "routed_to_meiai": meiai_active,
            },
            {
                "id": "meiai",
                "label": f"MeiAI/Proxy [{proxy_model}]",
                "active": meiai_active,
                "routed_to_meiai": meiai_active,
            },
            {"id": "openai",    "label": "OpenAI",      "active": bool(os.environ.get("OPENAI_API_KEY"))},
            {"id": "mistral",   "label": "MistralAI",   "active": bool(os.environ.get("MISTRAL_API_KEY"))},
            {"id": "google",    "label": "Google AI",   "active": bool(os.environ.get("GOOGLE_API_KEY"))},
            {"id": "azure",     "label": "Azure OpenAI","active": bool(os.environ.get("AZURE_API_KEY"))},
            {"id": "ollama",    "label": "Ollama",      "active": bool(os.environ.get("OLLAMA_API_BASE"))},
        ]
    }

@app.get("/api/v1/browser-config")
async def browser_config():
    """Get current browser configuration
    
    Note: Chrome paths (CHROME_PATH and CHROME_USER_DATA) can only be set via
    environment variables for security reasons and cannot be overridden in task requests.
    """
    headful = os.environ.get("BROWSER_USE_HEADFUL", "false").lower() == "true"
    chrome_path = os.environ.get("CHROME_PATH", None)
    chrome_user_data = os.environ.get("CHROME_USER_DATA", None)
    
    return {
        "headful": headful,
        "headless": not headful,
        "chrome_path": chrome_path,
        "chrome_user_data": chrome_user_data,
        "using_custom_chrome": chrome_path is not None,
        "using_user_data": chrome_user_data is not None
    }

@app.get("/api/v1/douyin/screenshot")
async def douyin_screenshot():
    """Capture a screenshot of the active browser tab"""
    from douyin_common import take_screenshot_bytes
    img_bytes = await take_screenshot_bytes()
    if img_bytes:
        return Response(content=img_bytes, media_type="image/png")
    raise HTTPException(status_code=404, detail="No active browser page found or browser is idle")

# In-memory store for scrape tasks
scrape_tasks: Dict[str, Dict] = {}


@app.post("/api/v1/douyin/channel-videos", response_model=ChannelVideoListResponse)
async def douyin_channel_videos(request: ChannelVideoListRequest):
    """
    Phase 1: Get video list from one or more channel pages. Returns immediately (~30s per URL).
    Also spawns Phase 2 in background — stream URLs processed while you receive this response.
    Poll GET /api/v1/douyin/stream-results/{task_id} for full results with stream URLs.
    """
    # Normalize input to list of URLs
    urls = [request.url] if isinstance(request.url, str) else request.url
    
    # Basic validation
    for url in urls:
        if "/user/" not in url:
            raise HTTPException(status_code=400, detail=f"URL must contain /user/: {url}")
            
    all_videos = []
    scraped_at = datetime.now(UTC).isoformat()
    
    # Process each URL sequentially
    for url in urls:
        try:
            result = await asyncio.wait_for(get_channel_video_list(url), timeout=180.0)
            all_videos.extend(result.videos)
        except asyncio.TimeoutError:
            logger.error(f"Channel scrape timed out for {url}")
            raise HTTPException(status_code=504, detail=f"Channel scrape timed out for {url}")
        except Exception as exc:
            logger.exception(f"Error scraping {url}")
            raise HTTPException(status_code=500, detail=f"Error scraping {url}: {str(exc)}")

    # Filter out paid videos unless explicitly requested
    is_paid_filter = request.is_paid if request.is_paid is not None else False
    if not is_paid_filter:
        filtered_videos = [v for v in all_videos if not v.is_paid]
    else:
        filtered_videos = all_videos

    task_id = str(uuid.uuid4())
    
    # Always spawn Phase 2 background task
    scrape_tasks[task_id] = {
        "stream_task_id": task_id,
        "status": "running",
        "channel_url": request.url,
        "total": len(filtered_videos),
        "completed": 0,
        "videos": [v.model_dump() for v in filtered_videos],
        "error": None,
        "created_at": datetime.now(UTC).isoformat(),
    }
    asyncio.create_task(run_phase2_background(task_id, scrape_tasks))

    return ChannelVideoListResponse(
        channel_url=request.url,
        scraped_at=scraped_at,
        total=len(filtered_videos),
        videos=filtered_videos,
        stream_task_id=task_id
    )


@app.get("/api/v1/douyin/stream-results/{task_id}")
async def douyin_stream_results(task_id: str):
    """
    Poll Phase 2 results. Returns current progress + all completed video details.
    status: running (still processing) | finished (all done) | failed
    """
    if task_id not in scrape_tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    
    # Confirm the background task to proceed if it hasn't already
    scrape_tasks[task_id]["confirmed"] = True
    
    task = scrape_tasks[task_id]
    return {
        "stream_task_id": task["stream_task_id"],
        "status": task["status"],
        "total": task["total"],
        "completed": task["completed"],
        "videos": task["videos"],
        "error": task.get("error"),
    }


@app.post("/api/v1/douyin/video-detail", response_model=VideoDetailResponse)
async def douyin_video_detail(request: VideoDetailRequest):
    """Get stream URLs + metadata for a single Douyin video. ~15-20s per call.
    Uses persistent browser (reuses session if channel was loaded recently)."""
    if "/video/" not in request.url:
        raise HTTPException(status_code=400, detail="URL must contain /video/")
    try:
        return await asyncio.wait_for(get_video_detail(request.url), timeout=60.0)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Video detail timed out")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


class DouyinScrapeRequest(BaseModel):
    url: Union[str, List[str]]  # single channel URL or list of URLs
    is_paid: Optional[bool] = None  # None/True = include paid videos; False = exclude paid videos


@app.post("/api/v1/scrape-douyin-channel")
async def scrape_douyin_channel_endpoint(request: DouyinScrapeRequest):
    """
    Full Douyin channel scrape — synchronous, returns result directly.
    Accepts single URL or list of channel URLs.
    
    is_paid:
      - true or omitted: include all videos (both free and paid)
      - false: exclude paid videos (is_paid=True) from results
    
    Note: Takes 5-15 minutes depending on number of videos.
    Set HTTP client timeout to at least 900s (15 min).
    """
    urls = [request.url] if isinstance(request.url, str) else request.url
    for u in urls:
        if "/user/" not in u:
            raise HTTPException(
                status_code=400,
                detail=f"URL must contain /user/: {u}",
            )
    include_paid = request.is_paid is not False  # True unless explicitly False
    try:
        result = await asyncio.wait_for(
            scrape_douyin_channel(request.url, include_paid=include_paid),
            timeout=900.0
        )
        return result
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Scrape timed out after 15 minutes")
    except Exception as exc:
        logger.exception("scrape-douyin-channel failed")
        raise HTTPException(status_code=500, detail=str(exc))


# Run server if executed directly
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port) 