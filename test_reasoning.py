import litellm


response = litellm.completion(
    model="gemini/gemini-2.5-pro",
    messages=[{"role": "user", "content": "Explain quantum physics in 1 paragraph"}],
    reasoning_effort="high",
)
msg = response.choices[0].message
print("---")
print(msg.model_dump())
