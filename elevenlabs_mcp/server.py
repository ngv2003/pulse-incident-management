from mcp.server.fastmcp import FastMCP
import requests
import os
from dotenv import load_dotenv
import json

load_dotenv()

API_KEY = os.getenv("ELEVENLABS_API_KEY")
VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")

mcp = FastMCP("elevenlabs")

@mcp.tool()
def elevenlabs_speak(text: str) -> str:
    """Convert text to speech using ElevenLabs"""

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}"
    
    if text.startswith("{"):
        text = json.loads(text)["text"]

    headers = {
        "xi-api-key": API_KEY,
        "Content-Type": "application/json",
        "accept": "audio/mpeg"
    }

    data = {
        "text": text,
        "model_id": "eleven_multilingual_v2"
    }

    response = requests.post(url, json=data, headers=headers)

    with open("speech.mp3", "wb") as f:
        f.write(response.content)

    return "Audio saved to speech.mp3"

if __name__ == "__main__":
    mcp.run()