
import json
import traceback
import logging
import os
import re
import base64
import time
import uuid
from datetime import datetime, timezone
from typing import Optional
# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Log environment info at cold start
logger.info("=" * 60)
logger.info("LAMBDA COLD START - Initializing...")
logger.info(f"Python version: {os.sys.version}")
logger.info(f"GOOGLE_API_KEY set: {'Yes' if os.environ.get('GOOGLE_API_KEY') else 'NO - MISSING!'}")
logger.info("=" * 60)

try:
    from google import genai
    from google.genai import types
    logger.info("Successfully imported google.genai SDK")
except ImportError as e:
    logger.error(f"Failed to import google.genai: {e}")
    raise

# Import libraries for URL fetching (Jaecoo tool)
try:
    import requests
    from bs4 import BeautifulSoup
    logger.info("Successfully imported requests and BeautifulSoup for Jaecoo tool")
except ImportError as e:
    logger.warning(f"Failed to import requests/BeautifulSoup: {e}. Jaecoo tool will not work.")
    requests = None
    BeautifulSoup = None

# TTS Model names to try - ordered by preference
# TTS model with native audio output (others: 404 or no audio support)
TTS_MODELS = [
    "gemini-2.5-flash-preview-tts",
]

from config import (
    GOOGLE_API_KEY,
    GEMINI_MODEL,
    GEMINI_TEMPERATURE,
    PERSONAS,
    DIFFICULTY_LEVELS,
    MODES,
    MODE_PROMPT_ADDONS,
    DEFAULT_PERSONA,
    DEFAULT_DIFFICULTY,
    DEFAULT_MODE,
    SYSTEM_PROMPT_TEMPLATE,
    FEEDBACK_PROMPT_TEMPLATE,
    CORS_HEADERS
)
from database import log_session_to_db

logger.info(f"Config loaded - Model: {GEMINI_MODEL}, API Key length: {len(GOOGLE_API_KEY) if GOOGLE_API_KEY else 0}")

# Voice mapping for personas (Gemini Journey Voices)
# Kore/Zephyr = Female, Fenrir/Puck/Charon = Male
PERSONA_VOICES = {
    "family": "Kore",      # Warm, balanced female
    "value": "Zephyr",     # Calm female  
    "electric": "Fenrir",  # Deep male
    "tech": "Puck",        # Energetic male
    "stressed": "Charon",  # Assertive male
}

# Initialize the GenAI client
try:
    if not GOOGLE_API_KEY:
        logger.error("GOOGLE_API_KEY is empty or not set!")
        raise ValueError("GOOGLE_API_KEY environment variable is not set")
    
    client = genai.Client(api_key=GOOGLE_API_KEY)
    logger.info("GenAI client initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize GenAI client: {e}")
    raise

# In-memory session storage (for POC - in production use DynamoDB/Redis)
chat_sessions = {}

# =============================================================================
# JAECOO MODEL INFO TOOL (Available only in exam mode)
# =============================================================================
JAECOO_MODEL_URLS = {
    "jaecoo7": "https://jaecoo.co.il/models/jaecoo7/",
    "jaecoo5": "https://jaecoo.co.il/models/jaecoo5/",
    "jaecoo8": "https://jaecoo.co.il/models/jaecoo8/"
}


def get_jaecoo_model_info(model_name: str) -> dict:
    """
    Fetch and extract information from Jaecoo model page.
    Available only in exam mode.
    
    Args:
        model_name: Model name (jaecoo7, jaecoo5, or jaecoo8)
        
    Returns:
        dict: Structured information about the model
    """
    logger.info("=" * 60)
    logger.info(f"🌐 JAECOO TOOL: Starting execution")
    logger.info(f"   Model requested: {model_name}")
    logger.info("=" * 60)
    
    if not requests or not BeautifulSoup:
        logger.error("❌ URL fetching libraries not available!")
        return {"error": "URL fetching libraries not available"}
    
    model_name_lower = model_name.lower() if model_name else ""
    logger.info(f"📝 Normalized model name: '{model_name_lower}'")
    
    url = JAECOO_MODEL_URLS.get(model_name_lower)
    
    if not url:
        logger.error(f"❌ Model '{model_name_lower}' not found in URL mapping!")
        logger.error(f"   Available models: {list(JAECOO_MODEL_URLS.keys())}")
        return {
            "error": f"Model {model_name} not found. Available models: jaecoo7, jaecoo5, jaecoo8"
        }
    
    logger.info(f"✅ Model found! URL: {url}")
    
    try:
        logger.info(f"📡 Making HTTP request to: {url}")
        # Set headers to mimic a browser
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        response = requests.get(url, headers=headers, timeout=10)
        logger.info(f"📥 HTTP Response: Status {response.status_code}")
        response.raise_for_status()
        
        logger.info(f"📄 Parsing HTML content...")
        logger.info(f"   Response size: {len(response.content)} bytes")
        # Parse HTML
        soup = BeautifulSoup(response.content, 'html.parser')
        
        logger.info(f"🧹 Cleaning HTML (removing scripts, styles, nav, footer, header)...")
        # Extract main content - remove scripts, styles, etc.
        for script in soup(["script", "style", "nav", "footer", "header"]):
            script.decompose()
        
        logger.info(f"📝 Extracting text content...")
        # Get text content
        text_content = soup.get_text(separator=' ', strip=True)
        
        logger.info(f"🧽 Cleaning up whitespace...")
        # Clean up excessive whitespace
        text_content = ' '.join(text_content.split())
        
        original_len = len(text_content)
        logger.info(f"   Original text length: {original_len} characters")
        
        # Limit content size to avoid token limits (keep first 8000 chars)
        if len(text_content) > 8000:
            text_content = text_content[:8000] + "..."
            logger.info(f"   ⚠️ Text truncated to 8000 chars (was {original_len})")
        
        logger.info(f"✅ Successfully extracted {len(text_content)} characters")
        logger.info(f"📊 Content preview (first 150 chars): {text_content[:150]}...")
        logger.info("=" * 60)
        
        return {
            "model": model_name_lower,
            "url": url,
            "content": text_content,
            "success": True
        }
        
    except requests.exceptions.Timeout:
        logger.error(f"⏱️ TIMEOUT: Request to {url} timed out after 10 seconds")
        return {
            "error": "Request timed out. The website may be slow or unavailable.",
            "model": model_name_lower,
            "url": url
        }
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ HTTP ERROR fetching URL {url}: {e}")
        logger.error(f"   Error type: {type(e).__name__}")
        return {
            "error": f"Failed to fetch information: {str(e)}",
            "model": model_name_lower,
            "url": url
        }
    except Exception as e:
        logger.error(f"❌ UNEXPECTED ERROR processing {url}: {e}")
        logger.error(f"   Error type: {type(e).__name__}")
        logger.error(traceback.format_exc())
        return {
            "error": f"Unexpected error: {str(e)}",
            "model": model_name_lower,
            "url": url
        }


def create_jaecoo_tool() -> types.Tool:
    """Create the Jaecoo model info tool declaration for Gemini."""
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="get_jaecoo_model_info",
                description=(
                    "Get detailed information about Jaecoo vehicle models (7, 5, or 8) from the official website. "
                    "Use this tool when the customer asks about Jaecoo models, specifications, features, prices, "
                    "or any details about Jaecoo 7, Jaecoo 5, or Jaecoo 8. "
                    "This tool fetches real-time information from the official Jaecoo website."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "model_name": types.Schema(
                            type=types.Type.STRING,
                            description="The Jaecoo model name. Must be one of: jaecoo7, jaecoo5, or jaecoo8",
                            enum=["jaecoo7", "jaecoo5", "jaecoo8"]
                        )
                    },
                    required=["model_name"]
                )
            )
        ]
    )


def create_json_response(status_code: int, body: dict) -> dict:
    """Create a properly formatted Lambda response with CORS headers."""
    response = {
        "statusCode": status_code,
        "headers": {
            **CORS_HEADERS,
            "Content-Type": "application/json"
        },
        "body": json.dumps(body, ensure_ascii=False)
    }
    logger.info(f"Response: status={status_code}, body_length={len(response['body'])}")
    return response


def get_persona_details(persona: str) -> dict:
    """Get persona details from config."""
    return PERSONAS.get(persona, PERSONAS[DEFAULT_PERSONA])


def get_difficulty_description(difficulty: str) -> str:
    """Get difficulty description from config."""
    return DIFFICULTY_LEVELS.get(difficulty, DIFFICULTY_LEVELS[DEFAULT_DIFFICULTY])


def get_mode_description(mode: str) -> str:
    """Get mode description from config."""
    return MODES.get(mode, MODES[DEFAULT_MODE])


def get_voice_for_persona(persona: str) -> str:
    """Get the TTS voice name for a persona."""
    return PERSONA_VOICES.get(persona, "Kore")


def clean_text_for_tts(text: str) -> str:
    """Clean text for TTS - remove stage directions, brackets, etc."""
    if not text:
        return ""
    
    clean = text
    # Remove content inside parentheses (e.g., (sighs), (laughing))
    clean = re.sub(r'\(.*?\)', '', clean)
    # Remove content inside brackets
    clean = re.sub(r'\[.*?\]', '', clean)
    # Remove content inside asterisks
    clean = re.sub(r'\*.*?\*', '', clean)
    # Remove special chars but keep punctuation
    clean = re.sub(r'[_<>@#$%^&+=]', '', clean)
    # Remove labels like "Client:" or "Me:"
    clean = re.sub(r'^(לקוח|אני|נציג|בוט|Customer|Me|Rep|Bot):', '', clean, flags=re.MULTILINE)
    # Clean up extra whitespace
    clean = ' '.join(clean.split())
    
    return clean.strip()


def generate_speech(text: str, persona: str, max_retries: int = 2) -> str:
    """Generate speech audio using Gemini TTS via the GenAI SDK.
    
    Returns:
        Base64-encoded audio data, or empty string on failure.
    """
    clean_text = clean_text_for_tts(text)
    
    if not clean_text or len(clean_text) < 2:
        logger.warning("Text was empty after cleaning, skipping TTS")
        return ""
    
    voice_name = get_voice_for_persona(persona)
    logger.info(f"Generating TTS with voice '{voice_name}' for text: {clean_text[:50]}...")
    
    # Try each TTS model in order
    for model_idx, tts_model in enumerate(TTS_MODELS):
        logger.info(f"Trying TTS model {model_idx + 1}/{len(TTS_MODELS)}: {tts_model}")
        
        # Retry loop for transient errors
        for attempt in range(max_retries + 1):
            try:
                # Check if this is a specialized TTS model
                is_tts_model = "tts" in tts_model.lower()
                
                # Build config based on model type
                if is_tts_model:
                    # TTS model requires explicit response_modalities=["AUDIO"]
                    # (default is TEXT, which causes 400 INVALID_ARGUMENT)
                    gen_config = {
                        "response_modalities": ["AUDIO"],
                        "speech_config": types.SpeechConfig(
                            voice_config=types.VoiceConfig(
                                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                    voice_name=voice_name
                                )
                            )
                        )
                    }
                else:
                    # For regular models, try with response_modalities
                    gen_config = {
                        "response_modalities": ["AUDIO"],
                        "speech_config": types.SpeechConfig(
                            voice_config=types.VoiceConfig(
                                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                    voice_name=voice_name
                                )
                            )
                        )
                    }
                    # Add system instruction for non-TTS models
                    gen_config["system_instruction"] = "You are a Text-to-Speech (TTS) engine. Output ONLY audio."
                
                # Use the SDK instead of manual HTTP requests
                response = client.models.generate_content(
                    model=tts_model,
                    contents=[
                        types.Content(
                            role="user",
                            parts=[types.Part(text=f"Convert this text to speech: {clean_text}")]
                        )
                    ],
                    config=types.GenerateContentConfig(**gen_config)
                )
                
                # Extract audio from response
                if response.candidates:
                    for candidate in response.candidates:
                        if candidate.content and candidate.content.parts:
                            for part in candidate.content.parts:
                                if part.inline_data:
                                    audio_bytes = part.inline_data.data
                                    mime_type = part.inline_data.mime_type
                                    if audio_bytes:
                                        # Ensure we return base64 string for JSON response
                                        if isinstance(audio_bytes, bytes):
                                            audio_data = base64.b64encode(audio_bytes).decode('utf-8')
                                        else:
                                            audio_data = audio_bytes
                                            
                                        logger.info(f"TTS success with model '{tts_model}', mime: {mime_type}, size: {len(audio_data)} chars")
                                        return audio_data
                
                logger.warning(f"Model {tts_model} did not return audio.")
                break  # Try next model
                
            except Exception as e:
                error_msg = str(e)
                logger.error(f"TTS Error ({tts_model}, attempt {attempt + 1}/{max_retries + 1}): {error_msg}")
                
                # If model not found or invalid model, try next model immediately
                if "404" in error_msg or "not found" in error_msg.lower() or "400" in error_msg:
                    logger.info(f"Model {tts_model} not available or invalid, trying next model...")
                    break
                
                # Retry on other errors (potentially transient)
                if attempt < max_retries:
                    wait_time = (attempt + 1) * 0.5
                    logger.info(f"Retrying TTS in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                
                break  # Try next model
    
    logger.warning("=" * 60)
    logger.warning("⚠️ TTS: All TTS models failed - returning empty audio")
    logger.warning("   This is not critical - the text response will still be returned")
    logger.warning("=" * 60)
    return ""


def build_system_prompt(persona: str, difficulty: str, mode: str) -> str:
    """Build the system prompt for the chat session."""
    persona_details = get_persona_details(persona)
    gender_instruction = "לשון נקבה" if persona_details["gender"] == "female" else "לשון זכר"
    
    return SYSTEM_PROMPT_TEMPLATE.format(
        gender_instruction=gender_instruction,
        persona_description=persona_details["description"],
        difficulty_description=get_difficulty_description(difficulty),
        mode_description=get_mode_description(mode),
        mode_prompt_addon=MODE_PROMPT_ADDONS.get(mode, MODE_PROMPT_ADDONS.get(DEFAULT_MODE, ""))
    )


def initialize_chat_session(
    session_id: str,
    persona: str,
    difficulty: str,
    mode: str,
    include_audio: bool = True,
    first_name: str = "",
    last_name: str = "",
) -> dict:
    """Initialize a new chat session."""
    logger.info(f"Initializing chat session: {session_id}")
    logger.info(f"Config - persona: {persona}, difficulty: {difficulty}, mode: {mode}, include_audio: {include_audio}")
    
    system_prompt = build_system_prompt(persona, difficulty, mode)
    logger.info(f"System prompt length: {len(system_prompt)} chars")
    
    try:
        # Create a chat session using the new SDK
        logger.info(f"Creating chat with model: {GEMINI_MODEL}")
        chat = client.chats.create(
            model=GEMINI_MODEL,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=GEMINI_TEMPERATURE,
            )
        )
        logger.info("Chat session created successfully")
    except Exception as e:
        logger.error(f"Failed to create chat session: {e}")
        logger.error(traceback.format_exc())
        raise
    
    # Store session data
    chat_sessions[session_id] = {
        "chat": chat,
        "config": {
            "persona": persona,
            "difficulty": difficulty,
            "mode": mode,
            "first_name": first_name or "",
            "last_name": last_name or "",
        },
        "messages": []
    }
    logger.info(f"Session stored. Total active sessions: {len(chat_sessions)}")
    
    # Get initial greeting from the AI
    try:
        logger.info("Sending initial message to get greeting...")
        response = chat.send_message("התחל את השיחה עכשיו.")
        greeting = response.text
        logger.info(f"Greeting received: {greeting[:100]}..." if len(greeting) > 100 else f"Greeting received: {greeting}")
    except Exception as e:
        logger.error(f"Failed to get greeting: {e}")
        logger.error(traceback.format_exc())
        raise
    
    # Store the greeting in messages
    chat_sessions[session_id]["messages"].append({
        "role": "model",
        "text": greeting
    })
    
    # Generate audio for the greeting
    audio_data = ""
    if include_audio:
        audio_data = generate_speech(greeting, persona)
    
    return {
        "session_id": session_id,
        "greeting": greeting,
        "audio": audio_data,
        "config": chat_sessions[session_id]["config"]
    }


def send_message(session_id: str, message: str, history: list, persona: str, difficulty: str, mode: str, include_audio: bool = True) -> dict:
    """Send a message with explicit chat history to Gemini.
    
    In exam mode, enables Jaecoo model info tool for function calling.
    """
    logger.info(f"Sending message to session: {session_id}")
    logger.info(f"Message: {message[:100]}..." if len(message) > 100 else f"Message: {message}")
    logger.info(f"History length: {len(history)}, persona: {persona}, difficulty: {difficulty}, mode: {mode}")

    # Ensure we have a stored session so feedback can rely on server-side transcript.
    #
    # Important: On AWS Lambda, in-memory state is NOT guaranteed between requests
    # (cold starts, parallel instances, etc.). If the session is missing, we rebuild
    # a minimal session from the client-provided `history` so the conversation keeps working
    # and feedback can still be generated from a server-side transcript.
    if session_id not in chat_sessions:
        logger.warning(f"Session not found in send_message (rebuilding from history): {session_id}")
        rebuilt_messages = []
        for msg in history or []:
            role = msg.get("role")
            text = (msg.get("text") or "").strip()
            if role in ("user", "model") and text:
                rebuilt_messages.append({"role": role, "text": text})

        chat_sessions[session_id] = {
            # We don't need `chat` for generate_content() calls here; keep placeholder.
            "chat": None,
            "config": {
                "persona": persona,
                "difficulty": difficulty,
                "mode": mode,
                "first_name": "",
                "last_name": "",
            },
            "messages": rebuilt_messages,
        }
        logger.info(
            f"Session rebuilt. Stored messages from history: {len(chat_sessions[session_id]['messages'])}"
        )
    
    # Build system prompt from persona, difficulty, mode
    system_prompt = build_system_prompt(persona, difficulty, mode)
    
    # Build contents array from history and new message
    contents = []
    
    # Add history messages
    for msg in history:
        role = msg.get("role", "user")
        text = msg.get("text", "")
        if role in ["user", "model"] and text:
            contents.append(
                types.Content(
                    role=role,
                    parts=[types.Part(text=text)]
                )
            )
    
    # Add current user message
    contents.append(
        types.Content(
            role="user",
            parts=[types.Part(text=message)]
        )
    )
    
    logger.info(f"Sending {len(contents)} message(s) to Gemini (including history)...")
    
    # Conditionally add tools only in exam mode
    tools = None
    if mode == "exam":
        logger.info("=" * 60)
        logger.info("🔧 TOOL SYSTEM: Exam mode detected - ENABLING Jaecoo model info tool")
        logger.info("=" * 60)
        tools = [create_jaecoo_tool()]
        logger.info(f"✅ Tool created and added to config. Available tool: get_jaecoo_model_info")
        logger.info(f"📋 Tool can fetch info from: {list(JAECOO_MODEL_URLS.keys())}")
    else:
        logger.info("=" * 60)
        logger.info("🚫 TOOL SYSTEM: Simulation mode - tools DISABLED")
        logger.info("=" * 60)
    
    # Build config with optional tools
    config_dict = {
        "system_instruction": system_prompt,
        "temperature": GEMINI_TEMPERATURE,
    }
    if tools:
        config_dict["tools"] = tools
        logger.info(f"📦 Config built with {len(tools)} tool(s) attached")
    else:
        logger.info("📦 Config built WITHOUT tools")
    
    # Function calling loop - continue until we get a final text response
    max_iterations = 5  # Prevent infinite loops
    iteration = 0
    ai_response = None
    
    try:
        while iteration < max_iterations:
            iteration += 1
            logger.info(f"Gemini API call iteration {iteration}/{max_iterations}")
            
            # Get AI response
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(**config_dict)
            )
            
            # Check if response has function calls
            function_calls = []
            logger.info(f"🔍 Analyzing response for function calls or text...")
            if response.candidates:
                logger.info(f"📥 Response has {len(response.candidates)} candidate(s)")
                for idx, candidate in enumerate(response.candidates):
                    logger.info(f"  Candidate {idx + 1}: checking parts...")
                    if candidate.content and candidate.content.parts:
                        logger.info(f"  Candidate {idx + 1}: has {len(candidate.content.parts)} part(s)")
                        for part_idx, part in enumerate(candidate.content.parts):
                            # Check for function call
                            if hasattr(part, 'function_call') and part.function_call:
                                logger.info(f"  ⚡ PART {part_idx + 1}: FUNCTION CALL DETECTED!")
                                logger.info(f"     Function name: {part.function_call.name}")
                                function_calls.append(part.function_call)
                            # Check for text response
                            if hasattr(part, 'text') and part.text:
                                logger.info(f"  📝 PART {part_idx + 1}: TEXT RESPONSE DETECTED")
                                logger.info(f"     Text length: {len(part.text)} chars")
                                ai_response = part.text
            else:
                logger.warning("⚠️ Response has no candidates!")
            
            # If we have function calls, execute them
            if function_calls:
                logger.info("=" * 60)
                logger.info(f"🛠️  FUNCTION CALLING: Gemini requested {len(function_calls)} function call(s)")
                logger.info("=" * 60)
                
                # Add model's function call request to conversation
                for call_idx, func_call in enumerate(function_calls):
                    logger.info(f"📞 Processing function call #{call_idx + 1}/{len(function_calls)}")
                    contents.append(
                        types.Content(
                            role="model",
                            parts=[types.Part(function_call=func_call)]
                        )
                    )
                    
                    # Execute the function
                    func_name = func_call.name
                    logger.info(f"  🔧 Function name: {func_name}")
                    
                    # Extract arguments - handle different possible structures
                    func_args = {}
                    if hasattr(func_call, 'args'):
                        logger.info(f"  📋 Function has args attribute")
                        if isinstance(func_call.args, dict):
                            func_args = func_call.args
                            logger.info(f"  ✅ Args is dict: {func_args}")
                        elif hasattr(func_call.args, '__dict__'):
                            func_args = func_call.args.__dict__
                            logger.info(f"  ✅ Args converted from object: {func_args}")
                        else:
                            # Try to convert to dict if it's a string (JSON)
                            try:
                                if isinstance(func_call.args, str):
                                    func_args = json.loads(func_call.args)
                                    logger.info(f"  ✅ Args parsed from JSON string: {func_args}")
                            except Exception as e:
                                logger.warning(f"  ⚠️ Failed to parse args as JSON: {e}")
                    else:
                        logger.warning(f"  ⚠️ Function call has no 'args' attribute")
                    
                    logger.info(f"  🎯 Executing function: {func_name}")
                    logger.info(f"  📥 Arguments received: {func_args}")
                    
                    # Handle Jaecoo tool
                    if func_name == "get_jaecoo_model_info":
                        logger.info("  🚗 Jaecoo tool detected - fetching model info...")
                        model_name = func_args.get("model_name", "") if isinstance(func_args, dict) else ""
                        if not model_name:
                            # Try alternative attribute access
                            model_name = getattr(func_call.args, 'model_name', '') if hasattr(func_call, 'args') else ""
                        
                        if model_name:
                            logger.info(f"  📍 Model requested: {model_name}")
                            logger.info(f"  🌐 URL: {JAECOO_MODEL_URLS.get(model_name.lower(), 'NOT FOUND')}")
                        else:
                            logger.error(f"  ❌ No model_name found in args!")
                        
                        tool_result = get_jaecoo_model_info(model_name)
                        
                        success = tool_result.get('success', False)
                        if success:
                            content_len = len(tool_result.get('content', ''))
                            logger.info(f"  ✅ Tool execution SUCCESS")
                            logger.info(f"  📊 Content length: {content_len} characters")
                            logger.info(f"  📦 Result keys: {list(tool_result.keys())}")
                        else:
                            error_msg = tool_result.get('error', 'Unknown error')
                            logger.error(f"  ❌ Tool execution FAILED: {error_msg}")
                    else:
                        logger.warning(f"  ⚠️ Unknown function: {func_name}")
                        tool_result = {"error": f"Unknown function: {func_name}"}
                    
                    # Add function result back to conversation
                    logger.info(f"  📤 Sending tool result back to Gemini...")
                    logger.info(f"  📏 Result size: {len(str(tool_result))} chars")
                    contents.append(
                        types.Content(
                            role="function",
                            parts=[types.Part(
                                function_response=types.FunctionResponse(
                                    name=func_name,
                                    response=tool_result
                                )
                            )]
                        )
                    )
                    logger.info(f"  ✅ Tool result added to conversation history")
                
                logger.info(f"  🔄 Continuing loop to get final response with tool results...")
                logger.info("=" * 60)
                # Continue loop to get final response with tool results
                continue
            
            # If we got a text response (no function calls), break
            if ai_response:
                logger.info("=" * 60)
                logger.info("✅ FINAL RESPONSE: No function calls, text response received")
                logger.info("=" * 60)
                logger.info(f"📝 Response text ({len(ai_response)} chars):")
                logger.info(f"   {ai_response[:200]}..." if len(ai_response) > 200 else f"   {ai_response}")
                break
            else:
                logger.info("  ℹ️ No text response yet, waiting for function calls to complete...")
        
        # If we exhausted iterations without a final response
        if not ai_response:
            logger.error("=" * 60)
            logger.error("❌ ERROR: Max iterations reached without final response")
            logger.error(f"   Completed {iteration} iterations")
            logger.error("=" * 60)
            ai_response = "מצטער, לא הצלחתי לקבל תשובה. נסה שוב."
        
    except Exception as e:
        logger.error(f"Failed to get AI response: {e}")
        logger.error(traceback.format_exc())
        raise
    
    # Generate audio for the response
    logger.info("=" * 60)
    logger.info("🎤 Generating audio for response...")
    audio_data = ""
    if include_audio:
        audio_data = generate_speech(ai_response, persona)
        if audio_data:
            logger.info(f"✅ Audio generated: {len(audio_data)} characters (base64)")
        else:
            logger.warning("⚠️ Audio generation returned empty")
    else:
        logger.info("⏭️ Audio generation skipped (include_audio=False)")
    
    logger.info("=" * 60)
    logger.info("✅ MESSAGE HANDLING COMPLETE")
    logger.info(f"   Session: {session_id}")
    logger.info(f"   Mode: {mode} {'(TOOLS ENABLED)' if mode == 'exam' else '(TOOLS DISABLED)'}")
    logger.info(f"   Response length: {len(ai_response)} chars")
    logger.info(f"   Iterations used: {iteration}/{max_iterations}")
    logger.info("=" * 60)

    # Persist turn to server-side transcript for feedback generation.
    # Feedback is generated from `chat_sessions[session_id]["messages"]`.
    try:
        chat_sessions[session_id]["messages"].append({"role": "user", "text": message})
        chat_sessions[session_id]["messages"].append({"role": "model", "text": ai_response})
        logger.info(
            f"🧾 Transcript updated. Total stored messages: {len(chat_sessions[session_id]['messages'])}"
        )
    except Exception as e:
        # Don't fail the request if transcript persistence fails; just log it.
        logger.error(f"Failed to persist transcript for session {session_id}: {e}")
        logger.error(traceback.format_exc())
    
    return {
        "session_id": session_id,
        "response": ai_response,
        "audio": audio_data
    }


# Hebrew mode labels for DynamoDB (UTF-8)
MODE_LABEL_FOR_DB = {
    "simulation": "סימולציה",
    "exam": "אימון",
}


def generate_feedback(
    session_id: str,
    cleanup_session: bool = False,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
) -> dict:
    """Generate feedback analysis for the conversation.
    
    Note:
        We intentionally do NOT auto-delete the session by default.
        Some clients send follow-up requests after fetching feedback, and deleting
        the session here can cause those requests to fail (e.g., 404 session not found),
        which may look like the app "exited" or "didn't get feedback".
    """
    logger.info(f"Generating feedback for session: {session_id}")
    logger.info(f"Feedback cleanup_session: {cleanup_session}")
    
    if session_id not in chat_sessions:
        logger.error(f"Session not found: {session_id}")
        raise ValueError(f"Session {session_id} not found")
    
    session = chat_sessions[session_id]
    config = session["config"]
    messages = session["messages"]
    
    logger.info(f"Session has {len(messages)} messages")
    
    # Build transcript
    transcript = "\n".join([
        f"{'User' if msg['role'] == 'user' else 'Model'}: {msg['text']}"
        for msg in messages
    ])

    # Log transcript (truncated to avoid CloudWatch spam / log size issues)
    try:
        transcript_preview_limit = 4000
        logger.info("=" * 60)
        logger.info("🧾 FEEDBACK TRANSCRIPT (preview)")
        logger.info(
            transcript[:transcript_preview_limit]
            + (f"\n...[truncated, total {len(transcript)} chars]..." if len(transcript) > transcript_preview_limit else "")
        )
        logger.info("=" * 60)
    except Exception as e:
        logger.warning(f"Failed to log transcript preview: {e}")
    
    mode_label = "מבחן" if config["mode"] == "exam" else "אימון"
    
    # Build feedback prompt
    feedback_prompt = FEEDBACK_PROMPT_TEMPLATE.format(
        mode=mode_label,
        persona=config["persona"],
        difficulty=config["difficulty"],
        transcript=transcript
    )
    
    logger.info(f"Feedback prompt length: {len(feedback_prompt)} chars")
    
    # Build conversation for feedback with optional Jaecoo tool (verify rep accuracy via website)
    contents = [
        types.Content(
            role="user",
            parts=[types.Part(text=feedback_prompt)]
        )
    ]
    feedback_config = types.GenerateContentConfig(
        temperature=0.3,
        tools=[create_jaecoo_tool()]
    )
    max_feedback_iterations = 8
    response_text = None
    
    try:
        logger.info("Calling Gemini for feedback analysis (with Jaecoo website verification tool)...")
        for iteration in range(max_feedback_iterations):
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
                config=feedback_config
            )
            # Check for function calls
            function_calls = []
            if response.candidates:
                for candidate in response.candidates:
                    if candidate.content and candidate.content.parts:
                        for part in candidate.content.parts:
                            if hasattr(part, 'function_call') and part.function_call:
                                function_calls.append(part.function_call)
                            if hasattr(part, 'text') and part.text:
                                response_text = part.text
            if function_calls:
                logger.info(f"🛠️ FEEDBACK: Gemini requested {len(function_calls)} tool call(s) for website verification")
                for func_call in function_calls:
                    func_name = func_call.name
                    func_args = {}
                    if hasattr(func_call, 'args'):
                        if isinstance(func_call.args, dict):
                            func_args = func_call.args
                        elif hasattr(func_call.args, '__dict__'):
                            func_args = func_call.args.__dict__
                        elif isinstance(getattr(func_call, 'args', None), str):
                            try:
                                func_args = json.loads(func_call.args)
                            except Exception:
                                pass
                    contents.append(
                        types.Content(
                            role="model",
                            parts=[types.Part(function_call=func_call)]
                        )
                    )
                    if func_name == "get_jaecoo_model_info":
                        model_name = func_args.get("model_name", "") if isinstance(func_args, dict) else ""
                        logger.info(f"  🌐 Fetching Jaecoo website info for model: {model_name}")
                        tool_result = get_jaecoo_model_info(model_name)
                    else:
                        tool_result = {"error": f"Unknown function: {func_name}"}
                    contents.append(
                        types.Content(
                            role="function",
                            parts=[types.Part(
                                function_response=types.FunctionResponse(
                                    name=func_name,
                                    response=tool_result
                                )
                            )]
                        )
                    )
                continue
            if response_text:
                logger.info(f"Feedback response received, length: {len(response_text)}")
                break
        if not response_text:
            logger.warning("Feedback loop ended without final text; using empty fallback")
            response_text = "{}"
        # Log raw model output (truncated)
        raw_preview_limit = 3000
        logger.info("=" * 60)
        logger.info("📥 FEEDBACK RAW RESPONSE (preview)")
        logger.info(
            response_text[:raw_preview_limit]
            + (f"\n...[truncated, total {len(response_text)} chars]..." if len(response_text) > raw_preview_limit else "")
        )
        logger.info("=" * 60)
    except Exception as e:
        logger.error(f"Failed to generate feedback: {e}")
        logger.error(traceback.format_exc())
        raise
    
    # Parse JSON (allow markdown code block)
    text_to_parse = response_text.strip()
    if "```json" in text_to_parse:
        start = text_to_parse.find("```json") + 7
        end = text_to_parse.find("```", start)
        text_to_parse = text_to_parse[start:end].strip() if end > start else text_to_parse
    elif "```" in text_to_parse:
        start = text_to_parse.find("```") + 3
        end = text_to_parse.find("```", start)
        text_to_parse = text_to_parse[start:end].strip() if end > start else text_to_parse
    try:
        feedback_data = json.loads(text_to_parse)
        if not isinstance(feedback_data, dict):
            feedback_data = {}
        if "accuracyVerification" not in feedback_data:
            feedback_data["accuracyVerification"] = {
                "usedWebsite": False,
                "modelsChecked": [],
                "correctClaims": [],
                "incorrectClaims": [],
                "summary": "לא בוצע אימות מול אתר (אין אזכור דגמי ג'אקו או שהמודל לא החזיר שדה זה)."
            }
        logger.info("Feedback JSON parsed successfully")
        # Log parsed feedback (keys + compact json preview)
        try:
            logger.info(f"📦 Parsed feedback keys: {list(feedback_data.keys()) if isinstance(feedback_data, dict) else type(feedback_data)}")
            parsed_preview_limit = 3000
            parsed_str = json.dumps(feedback_data, ensure_ascii=False)
            logger.info("=" * 60)
            logger.info("✅ FEEDBACK PARSED JSON (preview)")
            logger.info(
                parsed_str[:parsed_preview_limit]
                + (f"\n...[truncated, total {len(parsed_str)} chars]..." if len(parsed_str) > parsed_preview_limit else "")
            )
            logger.info("=" * 60)
        except Exception as e:
            logger.warning(f"Failed to log parsed feedback preview: {e}")
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse feedback JSON: {e}")
        logger.warning(f"Raw response: {response_text[:500] if response_text else 'N/A'}")
        feedback_data = {
            "error": "Failed to parse feedback",
            "raw_response": response_text or ""
        }
    
    # Log session to DynamoDB (gemini-trainer-agent)
    record_uuid = str(uuid.uuid4())
    timestamp_utc = datetime.now(timezone.utc).isoformat()
    config = session["config"]
    db_first = (first_name if first_name is not None else config.get("first_name", "")) or ""
    db_last = (last_name if last_name is not None else config.get("last_name", "")) or ""
    mode_for_db = MODE_LABEL_FOR_DB.get(config.get("mode", ""), "סימולציה")
    log_session_to_db({
        "uuid": record_uuid,
        "timestamp": timestamp_utc,
        "first_name": db_first,
        "last_name": db_last,
        "conversation_history": session["messages"],
        "feedback": feedback_data,
        "mode": mode_for_db,
    })
    
    # Optional cleanup (off by default)
    if cleanup_session:
        del chat_sessions[session_id]
        logger.info(f"Session {session_id} cleaned up. Remaining sessions: {len(chat_sessions)}")
    else:
        logger.info(
            f"Session {session_id} kept after feedback. Remaining sessions: {len(chat_sessions)}"
        )
    
    return {
        "session_id": session_id,
        "feedback": feedback_data
    }


def handle_log_session(body: dict) -> dict:
    """Log current session to DynamoDB with feedback=None (user left without requesting feedback)."""
    session_id = body.get("session_id")
    first_name = body.get("first_name")
    last_name = body.get("last_name")

    if not session_id:
        return create_json_response(400, {"success": False, "error": "Missing session_id"})

    if session_id not in chat_sessions:
        logger.warning(f"log_session: session not found {session_id}, skipping DB log")
        return create_json_response(200, {"success": True, "data": {"logged": False, "reason": "session_not_found"}})

    session = chat_sessions[session_id]
    config = session["config"]
    record_uuid = str(uuid.uuid4())
    timestamp_utc = datetime.now(timezone.utc).isoformat()
    db_first = (first_name if first_name is not None else config.get("first_name", "")) or ""
    db_last = (last_name if last_name is not None else config.get("last_name", "")) or ""
    mode_for_db = MODE_LABEL_FOR_DB.get(config.get("mode", ""), "סימולציה")

    log_session_to_db({
        "uuid": record_uuid,
        "timestamp": timestamp_utc,
        "first_name": db_first,
        "last_name": db_last,
        "conversation_history": session["messages"],
        "feedback": None,
        "mode": mode_for_db,
    })

    return create_json_response(200, {
        "success": True,
        "data": {"logged": True, "uuid": record_uuid}
    })


def handle_init(body: dict) -> dict:
    """Handle chat initialization request."""
    logger.info("Handling INIT action")
    
    session_id = body.get("session_id", f"session_{id(body)}")
    persona = body.get("persona", DEFAULT_PERSONA)
    difficulty = body.get("difficulty", DEFAULT_DIFFICULTY)
    mode = body.get("mode", DEFAULT_MODE)
    include_audio = body.get("include_audio", True)
    first_name = body.get("first_name", "")
    last_name = body.get("last_name", "")
    
    logger.info(f"Init params - session_id: {session_id}, persona: {persona}, difficulty: {difficulty}, mode: {mode}")
    
    result = initialize_chat_session(
        session_id, persona, difficulty, mode, include_audio,
        first_name=first_name, last_name=last_name,
    )
    return create_json_response(200, {
        "success": True,
        "data": result
    })


def handle_message(body: dict) -> dict:
    """Handle send message request."""
    logger.info("Handling MESSAGE action")
    
    session_id = body.get("session_id")
    message = body.get("message")
    history = body.get("history", [])
    persona = body.get("persona", DEFAULT_PERSONA)
    difficulty = body.get("difficulty", DEFAULT_DIFFICULTY)
    mode = body.get("mode", DEFAULT_MODE)
    include_audio = body.get("include_audio", True)
    
    logger.info(f"Message params - session_id: {session_id}, message_length: {len(message) if message else 0}, history_length: {len(history)}, persona: {persona}, difficulty: {difficulty}, mode: {mode}")
    
    if not session_id or not message:
        logger.warning("Missing session_id or message")
        return create_json_response(400, {
            "success": False,
            "error": "Missing session_id or message"
        })
    
    try:
        result = send_message(session_id, message, history, persona, difficulty, mode, include_audio)
        return create_json_response(200, {
            "success": True,
            "data": result
        })
    except ValueError as e:
        logger.error(f"ValueError in send_message: {e}")
        return create_json_response(404, {
            "success": False,
            "error": str(e)
        })
    except Exception as e:
        logger.error(f"Exception in send_message: {e}")
        logger.error(traceback.format_exc())
        return create_json_response(500, {
            "success": False,
            "error": str(e)
        })


def handle_feedback(body: dict) -> dict:
    """Handle feedback generation request."""
    logger.info("Handling FEEDBACK action")
    
    session_id = body.get("session_id")
    logger.info(f"Feedback params - session_id: {session_id}")
    cleanup_session = bool(body.get("cleanup_session", False))
    first_name = body.get("first_name")
    last_name = body.get("last_name")
    history = body.get("history", [])
    persona = body.get("persona", DEFAULT_PERSONA)
    difficulty = body.get("difficulty", DEFAULT_DIFFICULTY)
    mode = body.get("mode", DEFAULT_MODE)
    
    if not session_id:
        logger.warning("Missing session_id")
        return create_json_response(400, {
            "success": False,
            "error": "Missing session_id"
        })
    
    # Rebuild session from client-provided history when missing (e.g. different Lambda instance)
    if session_id not in chat_sessions and history:
        logger.warning(f"Session not found in handle_feedback (rebuilding from history): {session_id}")
        rebuilt_messages = []
        for msg in history:
            role = msg.get("role")
            text = (msg.get("text") or "").strip()
            if role in ("user", "model") and text:
                rebuilt_messages.append({"role": role, "text": text})
        chat_sessions[session_id] = {
            "chat": None,
            "config": {
                "persona": persona,
                "difficulty": difficulty,
                "mode": mode,
                "first_name": (first_name or "").strip() if first_name is not None else "",
                "last_name": (last_name or "").strip() if last_name is not None else "",
            },
            "messages": rebuilt_messages,
        }
        logger.info(f"Rebuilt session from history: {len(rebuilt_messages)} messages")
    
    try:
        result = generate_feedback(
            session_id,
            cleanup_session=cleanup_session,
            first_name=first_name,
            last_name=last_name,
        )
        return create_json_response(200, {
            "success": True,
            "data": result
        })
    except ValueError as e:
        logger.error(f"ValueError in generate_feedback: {e}")
        return create_json_response(404, {
            "success": False,
            "error": str(e)
        })


def lambda_handler(event, context):
    """
    Main Lambda handler function.
    
    Routes requests based on the 'action' parameter:
    - 'init': Initialize a new chat session
    - 'message': Send a message to existing session
    - 'feedback': Generate feedback for the conversation
    
    Args:
        event: API Gateway event
        context: Lambda context
        
    Returns:
        dict: Response with statusCode, headers, and body
    """
    logger.info("=" * 60)
    logger.info("LAMBDA HANDLER INVOKED")
    logger.info(f"Request ID: {context.aws_request_id if context else 'N/A'}")
    logger.info(f"Event type: {type(event)}")
    logger.info(f"Event keys: {list(event.keys()) if isinstance(event, dict) else 'N/A'}")
    logger.info(f"HTTP Method: {event.get('httpMethod', 'N/A')}")
    logger.info(f"Path: {event.get('path', 'N/A')}")
    logger.info(f"Body type: {type(event.get('body'))}")
    logger.info(f"Body preview: {str(event.get('body', ''))[:200]}")
    logger.info("=" * 60)
    
    # Handle CORS preflight
    if event.get("httpMethod") == "OPTIONS":
        logger.info("Handling OPTIONS preflight request")
        return create_json_response(200, {"message": "OK"})
    
    # Parse request body - handle both API Gateway proxy and direct invocation
    try:
        # Check if this is an API Gateway event (has 'body' key) or direct invocation
        if "body" in event and event.get("httpMethod"):
            # API Gateway with Lambda Proxy integration
            body_str = event.get("body", "{}")
            if body_str is None:
                body_str = "{}"
                logger.warning("Body was None, using empty object")
            
            if isinstance(body_str, str):
                body = json.loads(body_str)
                logger.info(f"Parsed body from API Gateway string: {list(body.keys())}")
            else:
                body = body_str
                logger.info(f"Body was already dict from API Gateway")
        elif "action" in event:
            # Direct Lambda invocation or Lambda console test - event IS the body
            body = event
            logger.info(f"Using event directly as body (direct invocation): {list(body.keys())}")
        else:
            # Try to parse body field anyway
            body_str = event.get("body", "{}")
            if body_str is None:
                body_str = "{}"
            if isinstance(body_str, str):
                body = json.loads(body_str)
            else:
                body = body_str or {}
            logger.info(f"Fallback body parsing: {list(body.keys()) if body else 'empty'}")
            
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error: {e}")
        logger.error(f"Event: {json.dumps(event)[:500]}")
        return create_json_response(400, {
            "success": False,
            "error": f"Invalid JSON in request body: {str(e)}"
        })
    
    # Route based on action
    action = body.get("action", "")
    session_id = body.get("session_id")
    client_context = body.get("client_context")
    if client_context:
        logger.info(f"CLIENT_CONTEXT (from frontend): session_id={client_context.get('session_id')} message_count={client_context.get('message_count')} ts={client_context.get('ts')} action={client_context.get('action')}")
    client_logs = body.get("client_logs") or []
    if isinstance(client_logs, list) and client_logs:
        logger.info(f"FRONTEND CONSOLE LOGS ({len(client_logs)} entries):")
        for line in client_logs[:30]:  # cap at 30 lines per request
            if isinstance(line, str):
                logger.info(f"  [FRONTEND] {line}")
            else:
                logger.info(f"  [FRONTEND] {str(line)[:500]}")
    logger.info(f"Action requested: '{action}' session_id={session_id}")
    if action == "message" and isinstance(body.get("history"), list):
        logger.info(f"Message request: history_length={len(body['history'])}")
    if action == "feedback" and isinstance(body.get("history"), list):
        logger.info(f"Feedback request: history_length={len(body['history'])}")
    
    if not action:
        logger.warning("No action specified in request")
        return create_json_response(400, {
            "success": False,
            "error": "Missing 'action' in request body. Valid actions: init, message, feedback, log_session"
        })
    
    try:
        if action == "init":
            return handle_init(body)
        elif action == "message":
            return handle_message(body)
        elif action == "feedback":
            return handle_feedback(body)
        elif action == "log_session":
            return handle_log_session(body)
        else:
            logger.warning(f"Unknown action: {action}")
            return create_json_response(400, {
                "success": False,
                "error": f"Unknown action: {action}. Valid actions: init, message, feedback, log_session"
            })
    except Exception as e:
        logger.error(f"Unhandled exception: {str(e)}")
        logger.error(f"Exception type: {type(e).__name__}")
        logger.error(f"Traceback:\n{traceback.format_exc()}")
        return create_json_response(500, {
            "success": False,
            "error": f"Internal server error: {str(e)}"
        })