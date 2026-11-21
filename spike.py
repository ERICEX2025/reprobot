"""30-minute check: can a Gemini managed agent open a website in its sandbox?"""
import sys, time
from google import genai

beta = genai.Client(http_options={"api_version": "v1beta"})  # Managed Agents live on v1beta

task = sys.argv[1] if len(sys.argv) > 1 else (
    "Open https://example.com in a real browser inside your sandbox. "
    "Tell me the page title, the main heading text, and exactly which tool or library you used to load it."
)

t0 = time.time()
run = beta.interactions.create(
    agent="antigravity-preview-09-2026",
    environment="remote",
    agent_config={"type": "antigravity", "model": "gemini-4-hydrogen", "max_total_tokens": 60000},
    input=task,
)
# The guide prints output_text directly; poll in case it comes back still running.
while getattr(run, "status", None) in ("in_progress", "queued"):
    time.sleep(3)
    run = beta.interactions.get(id=run.id)

print(f"status={getattr(run, 'status', '?')}  seconds={time.time() - t0:.0f}  sandbox={getattr(run, 'environment_id', '?')}")
print(run.output_text)
