"""
One place for every LLM call (Groq). Shared by BI-02 now, BI-03 / LC-01 later.

  - key comes from backend/.env  (GROQ_API_KEY), never from code or chat
  - temperature 0 + seed + on-disk cache  -> same input gives same output (reproducible labels)
  - retries on rate limits / network errors, honours Groq's retry-after
  - STATS counts calls, cache hits and tokens (shown on the metrics page later)

Quick test:  python groq_client.py
"""
import hashlib
import json
import os
import threading
import time
from collections import deque
from pathlib import Path

from dotenv import load_dotenv
from groq import APIConnectionError, APIStatusError, Groq, RateLimitError

HERE = Path(__file__).resolve().parent
load_dotenv(HERE.parent / ".env")

MODEL_FAST = os.getenv("GROQ_MODEL_FAST", "openai/gpt-oss-20b")     # extraction, easy steps
MODEL_SMART = os.getenv("GROQ_MODEL_SMART", "openai/gpt-oss-120b")  # final decision
REASONING = os.getenv("GROQ_REASONING_EFFORT", "low")                # gpt-oss: low | medium | high
CACHE_DIR = Path(os.getenv("LLM_CACHE_DIR", HERE / "cache"))
SEED = 42
MAX_RETRIES = 10

MAX_WAIT_S = int(os.getenv("GROQ_MAX_WAIT_S", "45"))  # a longer rate-limit wait fails fast instead of hanging the UI


def _log(msg: str) -> None:
    print(f"[groq] {msg}", flush=True)


STATS = {"calls": 0, "cache_hits": 0, "retries": 0, "prompt_tokens": 0, "completion_tokens": 0}
_client: Groq | None = None

# Groq's free tier allows ~8,000 tokens per minute per model. Stay under it instead of bouncing off 429s.
TPM_BUDGET = int(os.getenv("GROQ_TPM_BUDGET", "6500"))
_LOCK = threading.Lock()
_USED: dict[str, deque] = {}


def _reserve(model: str, est: int) -> list:
    """Block until `est` tokens fit in the rolling 60 s budget of this model. Returns the entry to correct later."""
    while True:
        with _LOCK:
            q = _USED.setdefault(model, deque())
            now = time.time()
            while q and now - q[0][0] > 60:
                q.popleft()
            if not q or sum(t for _, t in q) + est <= TPM_BUDGET:
                entry = [now, est]
                q.append(entry)
                return entry
            wait = 60 - (now - q[0][0]) + 0.2
        time.sleep(max(wait, 0.5))


def _get_client() -> Groq:
    global _client
    if _client is None:
        key = os.getenv("GROQ_API_KEY")
        if not key:
            raise RuntimeError("GROQ_API_KEY missing. Put it in backend/.env (see .env.example).")
        # max_retries=0: the SDK would retry 429s silently; we retry ourselves so every wait is logged
        _client = Groq(api_key=key, timeout=60, max_retries=0)
    return _client


def _cache_path(model: str, system: str, user: str, reasoning: str) -> Path:
    h = hashlib.sha256(f"{model}\n{reasoning}\n{system}\n{user}".encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{h}.json"


def chat_json(system: str, user: str, model: str | None = None, use_cache: bool = True,
              reasoning: str | None = None) -> dict:
    """Ask the model for a JSON object and return it as a dict."""
    model = model or MODEL_FAST
    reasoning = reasoning or REASONING
    system = system.rstrip() + "\n\nRespond with a single valid JSON object and nothing else."
    path = _cache_path(model, system, user, reasoning)
    if use_cache and path.exists():
        STATS["cache_hits"] += 1
        return json.loads(path.read_text(encoding="utf-8"))

    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for attempt in range(MAX_RETRIES):
        try:
            t_wait = time.time()
            entry = _reserve(model, (len(system) + len(user)) // 3 + 800)  # prompt estimate + room for the answer
            t_api = time.time()
            resp = _get_client().chat.completions.create(
                model=model, messages=messages, temperature=0, seed=SEED,
                reasoning_effort=reasoning, response_format={"type": "json_object"})
            STATS["calls"] += 1
            if resp.usage:
                entry[1] = resp.usage.total_tokens  # replace the estimate with the real number
                STATS["prompt_tokens"] += resp.usage.prompt_tokens
                STATS["completion_tokens"] += resp.usage.completion_tokens
            result = json.loads(resp.choices[0].message.content)
            _log(f"{model} ok | waited {t_api - t_wait:.1f}s for token budget | api call {time.time() - t_api:.1f}s | {entry[1]} tokens")
            if use_cache:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            return result
        except RateLimitError as e:
            wait = float(e.response.headers.get("retry-after", 0) or 0) or min(2 ** attempt, 60)
            _log(f"{model} RATE LIMITED (429): {str(e)[:220]}")
            if wait > MAX_WAIT_S:
                raise RuntimeError(f"Groq rate limit reached. Try again in about {int(wait)}s.") from e
        except (APIConnectionError, json.JSONDecodeError):
            wait = min(2 ** attempt, 10)
        except APIStatusError as e:
            if e.status_code < 500:  # 4xx (bad model name, bad key ...) will not fix itself
                raise
            wait = min(2 ** attempt, 10)
        STATS["retries"] += 1
        _log(f"{model} retry {attempt + 1}/{MAX_RETRIES} in {wait:.0f}s")
        time.sleep(wait)
    raise RuntimeError(f"Groq call failed after {MAX_RETRIES} attempts")


if __name__ == "__main__":
    out = chat_json("You reply in JSON.", 'Return {"ok": true, "model_says": "<one short greeting>"}', use_cache=False)
    print(out, "| models:", MODEL_FAST, "/", MODEL_SMART, "| stats:", STATS)
