#!/usr/bin/env python3
"""Generate slide narration with Gradium and assemble a two-minute presentation.

Reads GRADIUM_API_KEY from env.conf; never writes or logs authentication headers.
Reference: https://docs.gradium.ai/guides/text-to-speech-rest
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import wave

import requests

ROOT = Path(__file__).resolve().parents[1]
PRESENTATION = ROOT / "outputs/final-sprint/presentation"
ENDPOINT = "https://api.gradium.ai/api/post/speech/tts"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n")
    temp.replace(path)


def credentials():
    for line in (ROOT / "env.conf").read_text().splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip().removeprefix("export ") == "GRADIUM_API_KEY":
            key = value.strip().strip("\"'")
            if key:
                return key
    key = os.environ.get("GRADIUM_API_KEY")
    if not key:
        raise ValueError("GRADIUM_API_KEY is missing")
    return key


def duration(path):
    with wave.open(str(path), "rb") as audio:
        count = 0
        while chunk := audio.readframes(48000):
            count += len(chunk)
        return count / (audio.getnchannels() * audio.getsampwidth() * audio.getframerate())


def normalize_wav(path):
    """Replace streaming size placeholders with the real PCM length."""
    fixed = path.with_suffix(".fixed.wav")
    with wave.open(str(path), "rb") as source, wave.open(str(fixed), "wb") as target:
        target.setnchannels(source.getnchannels())
        target.setsampwidth(source.getsampwidth())
        target.setframerate(source.getframerate())
        while chunk := source.readframes(48000):
            target.writeframesraw(chunk)
    fixed.replace(path)


def run(*args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice-id", default="nCb6TznGzIsRjsoK")
    args = parser.parse_args()
    key = credentials()
    data = json.loads((PRESENTATION / "narration.json").read_text())
    slide_count = len(data["slides"])
    audio = PRESENTATION / "audio"
    raw = audio / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    state = {"status": "generating", "voice_id": args.voice_id, "model_name": "default",
             "target_seconds": 120, "planned_slides": slide_count, "completed_slides": 0, "segments": []}
    save(audio / "state.json", state)
    for index, slide in enumerate(data["slides"], 1):
        payload = {"voice_id": args.voice_id, "model_name": "default", "output_format": "wav",
                   "only_audio": True, "text": slide["narration"],
                   "json_config": {"rewrite_rules": "en", "padding_bonus": 0.0}}
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        path = raw / f"slide-{index:02d}.wav"
        metadata_path = raw / f"slide-{index:02d}.json"
        cached = path.exists() and metadata_path.exists() and json.loads(metadata_path.read_text()).get("request_sha256") == fingerprint
        if not cached:
            save(raw / f"slide-{index:02d}-request.json", payload)
            for attempt in range(1, 4):
                try:
                    response = requests.post(ENDPOINT, json=payload, headers={"x-api-key": key},
                                             timeout=(15, 180), allow_redirects=False)
                    if response.status_code != 200:
                        if (response.status_code == 429 or response.status_code >= 500) and attempt < 3:
                            time.sleep(5)
                            continue
                        raise RuntimeError(f"Gradium returned HTTP {response.status_code}; no key or response body logged")
                    path.write_bytes(response.content)
                    normalize_wav(path)
                    seconds = duration(path)
                    save(metadata_path, {"request_sha256": fingerprint, "voice_id": args.voice_id,
                         "model_name": "default", "duration_seconds": seconds,
                         "content_type": response.headers.get("Content-Type"),
                         "audio_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
                    break
                except (requests.Timeout, requests.ConnectionError):
                    if attempt == 3:
                        raise RuntimeError(f"Gradium transport failed for slide {index} after three attempts") from None
                    time.sleep(5)
        normalize_wav(path)
        seconds = duration(path)
        metadata = json.loads(metadata_path.read_text())
        metadata.update(duration_seconds=seconds, audio_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        wav_headers="Normalized to actual PCM sample count")
        save(metadata_path, metadata)
        state["segments"].append({"slide": index, "title": slide["title"], "raw_seconds": seconds,
                                  "raw_file": str(path.relative_to(PRESENTATION))})
        state["completed_slides"] = index
        save(audio / "state.json", state)
        print(f"Slide {index}/{slide_count}: {seconds:.2f} seconds ready", flush=True)

    total = sum(s["raw_seconds"] for s in state["segments"])
    tempo = total / 120
    if not 0.75 <= tempo <= 1.3:
        state.update(status="needs_pacing_review", raw_total_seconds=total, proposed_tempo=tempo)
        save(audio / "state.json", state)
        raise RuntimeError(f"Narration needs a pacing review: raw total {total:.2f}s")
    concat = raw / "concat.txt"
    concat.write_text("\n".join(f"file 'slide-{i:02d}.wav'" for i in range(1, slide_count + 1)) + "\n")
    run("ffmpeg", "-y", "-f", "concat", "-safe", "1", "-i", str(concat), "-af",
        f"atempo={tempo:.10f},apad,atrim=duration=120", "-ar", "48000", "-ac", "1",
        "-c:a", "pcm_s16le", str(PRESENTATION / "narration.wav"))
    run("ffmpeg", "-y", "-i", str(PRESENTATION / "narration.wav"), "-c:a", "libmp3lame",
        "-b:a", "192k", str(PRESENTATION / "narration.mp3"))
    elapsed = 0.0
    for segment, slide in zip(state["segments"], data["slides"]):
        segment["start_s"] = elapsed
        elapsed += segment["raw_seconds"] / tempo
        segment["stop_s"] = elapsed
        slide["audio_start_s"], slide["audio_stop_s"] = segment["start_s"], elapsed
    state.update(status="audio_ready", raw_total_seconds=total, uniform_tempo_factor=tempo,
                 actual_audio_seconds=duration(PRESENTATION / "narration.wav"),
                 processing="Uniform pitch-preserving tempo adjustment to 120 seconds")
    data["actual_duration_seconds"] = state["actual_audio_seconds"]
    data["voice_id"] = args.voice_id
    save(PRESENTATION / "narration.json", data)
    save(audio / "state.json", state)

    # Render the static PDF at video resolution, then align slides to the voice.
    frames = PRESENTATION / "video-frames"
    frames.mkdir(exist_ok=True)
    shutil.copyfile(PRESENTATION / "build/final-sprint.pdf", PRESENTATION / "final-sprint.pdf")
    run("pdftoppm", "-png", "-scale-to-x", "1920", "-scale-to-y", "1080",
        str(PRESENTATION / "final-sprint.pdf"), str(frames / "slide"))
    lines = []
    for index, segment in enumerate(state["segments"], 1):
        lines.extend([f"file 'slide-{index}.png'", f"duration {segment['stop_s'] - segment['start_s']:.10f}"])
    lines.append(f"file 'slide-{slide_count}.png'")
    timeline = frames / "timeline.txt"
    timeline.write_text("\n".join(lines) + "\n")
    run("ffmpeg", "-y", "-f", "concat", "-safe", "1", "-i", str(timeline),
        "-i", str(PRESENTATION / "narration.wav"), "-t", "120", "-r", "30",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-b:a", "192k", "-movflags", "+faststart", str(PRESENTATION / "final-sprint-narrated.mp4"))
    state["status"] = "finished"
    save(audio / "state.json", state)
    print(json.dumps({"status": "finished", "voice_id": args.voice_id, "raw_seconds": total,
                      "tempo_factor": tempo, "final_seconds": state["actual_audio_seconds"],
                      "video": str(PRESENTATION / "final-sprint-narrated.mp4")}), flush=True)


if __name__ == "__main__":
    main()
