"""Opt-in actual HA TTS entity -> HTTP -> isolated OC speech runtime proof.

Called by the companion Node harness with synthetic provider and local audio.
First bytes, not audible playback, release the delayed final text.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from importlib.metadata import version
import platform

import aiohttp
from homeassistant.components.tts import TTSAudioRequest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from custom_components.openclaw_voice.client import VoiceClient
from custom_components.openclaw_voice.tts import OpenClawTextToSpeech


async def main():
    first_audio = asyncio.Event()
    times = {}

    async def text():
        times["opening_text"] = time.monotonic()
        yield "The opening sentence."
        await first_audio.wait()
        times["final_text"] = time.monotonic()
        yield " This is the delayed ending."

    async with aiohttp.ClientSession() as session:
        client = VoiceClient(session, os.environ["VOICE_PROOF_URL"],
                             os.environ["VOICE_PROOF_TOKEN"], os.environ.get("VOICE_PROOF_REVISION", "fixture"))
        entity = OpenClawTextToSpeech(client, "fixture", ["en"], "en")
        assert entity.async_supports_streaming_input()
        async with asyncio.timeout(20):
            response = await entity.async_stream_tts_audio(TTSAudioRequest("en", {}, text()))
            assert response.extension == "wav"
            audio = bytearray()
            async for chunk in response.data_gen:
                if not first_audio.is_set():
                    assert "final_text" not in times, "audio arrived only after final text"
                    times["first_audio"] = time.monotonic()
                    first_audio.set()
                audio.extend(chunk)
        assert times["first_audio"] < times["final_text"]
        Path(os.environ["VOICE_PROOF_OUTPUT"]).write_bytes(audio)
        print(json.dumps({"status": "passed", "bytes": len(audio), "extension": response.extension,
                          "homeassistant": version("homeassistant"), "python": platform.python_version(),
                          "early_audio": True, "audible_playback_tested": False,
                          "first_audio_ms": round(1000 * (times["first_audio"] - times["opening_text"]), 1)}))


asyncio.run(main())
