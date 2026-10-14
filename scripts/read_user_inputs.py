import json
import sys

sys.stdout.reconfigure(encoding='utf-8')
transcript_path = r"C:\Users\sadegh\.gemini\antigravity\brain\f9dcd7bb-2b72-44bf-ab0f-909e1a684175\.system_generated\logs\transcript.jsonl"

with open(transcript_path, "r", encoding="utf-8", errors="ignore") as f:
    for idx, line in enumerate(f):
        if '"USER_INPUT"' in line:
            data = json.loads(line)
            print(f"[{idx}] {data.get('content')}")
