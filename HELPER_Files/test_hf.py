# save as test_hf.py
from llm_gateway import LLMGateway
gw = LLMGateway(provider="huggingface")
gw.initialize()
result = gw.chat(
    "Write exactly 3 lines of Gherkin for a login form test.",
    system_prompt="You are a QA engineer. Output only Gherkin, no commentary.",
    model_override="mistralai/Mistral-7B-Instruct-v0.3",
    temperature=0.2,
    timeout=60,
)
print(result)