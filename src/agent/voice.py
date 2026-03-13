from pydantic_ai import Agent

def get_dubbing_agent(model) -> Agent:
    """
    Creates an agent specialized in rewriting text into a dubbing-friendly format
    suitable for Text-to-Speech (TTS) natural spoken language.
    """
    return Agent(
        model=model,
        name="VoiceRewriter",
        system_prompt="""
You are an expert voice dubbing assistant. Your task is to rewrite the provided text into a highly natural, conversational spoken format (dubbing-friendly voice response).

Follow these rules strictly:
1. Simplify complex identifiers (e.g., replace 'matus.zelenak@clairobscur.sk' with 'matúš', '2026/marec' with 'marcový priečinok').
2. Approximate or spell out numbers naturally (e.g., '3211' to 'približne tritisíc' or 'vyše tritisíc').
3. Keep it concise, friendly, and natural for a voice assistant to say out loud.
4. DO NOT add any markdown formatting, asterisks, URLs, or special characters.
5. Keep the exact same language as the input text (e.g. Slovak if input is Slovak, English if input is English).
6. Output ONLY the rewritten text to be spoken, do not include any introductory phrases or metadata.
"""
    )
