from pydantic import BaseModel, Field
from pydantic_ai import Agent


class DubbingResult(BaseModel):
    """Structured output of the voice-dubbing agent.

    `language` is a 2-letter ISO 639-1 code (e.g. "sk", "en") passed straight to
    Cartesia TTS so the synthesized speech matches the reply language instead of
    relying on Cartesia's auto-detect (which produced Slovak with an English
    accent when the language was not specified).
    """

    spoken_text: str = Field(
        description="The text rewritten into a natural, dubbing-friendly spoken format. No markdown, URLs, or special characters."
    )
    language: str = Field(
        description=(
            "Two-letter ISO 639-1 language code of the spoken text, e.g. 'sk' for Slovak, 'en' for English. "
            "Must match the language the reply is actually written in."
        ),
    )


def get_dubbing_agent(model) -> Agent[DubbingResult]:
    """Create an agent that rewrites text for voice and reports its language.

    Returns a structured ``DubbingResult`` so the caller can forward the language
    code to Cartesia TTS.
    """
    return Agent(
        model=model,
        name="VoiceRewriter",
        output_type=DubbingResult,
        system_prompt=(
            "You are an expert voice dubbing assistant. Your task is to rewrite the provided text into a "
            "highly natural, conversational spoken format (dubbing-friendly voice response).\n\n"
            "Follow these rules strictly:\n"
            "1. Simplify complex identifiers (e.g., replace 'matus.zelenak@clairobscur.sk' with 'matúš', "
            "'2026/marec' with 'marcový priečinok').\n"
            "2. Approximate or spell out numbers naturally (e.g., '3211' to 'približne tritisíc' or "
            "'vyše tritisíc').\n"
            "3. Keep it concise, friendly, and natural for a voice assistant to say out loud.\n"
            "4. DO NOT add any markdown formatting, asterisks, URLs, or special characters.\n"
            "5. Keep the exact same language as the input text (e.g. Slovak if input is Slovak, English "
            "if input is English).\n"
            "6. In `spoken_text`, output ONLY the rewritten text to be spoken, with no introductory "
            "phrases or metadata.\n"
            "7. In `language`, return the two-letter ISO 639-1 code of the language you wrote "
            "`spoken_text` in (e.g. 'sk', 'en', 'de', 'cs', 'fr', 'es', 'it', 'hu', 'pl', 'ru', 'uk', "
            "'ja', 'ko', 'zh', 'ar', 'hi', 'pt', 'nl', 'tr')."
        ),
    )
