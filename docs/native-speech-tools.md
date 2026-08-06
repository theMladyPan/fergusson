# Native speech tools

The router now escalates all speech tasks to the Core Agent, which exposes `transcribe_audio` for Chirp 3 STT and `synthesize_speech` for Cartesia TTS, with prompt guidance forbidding bash, ffmpeg, Whisper, pip, or ad-hoc Python fallbacks. `send_message_to_channel` accepts optional `media_paths`, allowing an MP3 returned by `synthesize_speech` to be delivered directly; this was added because STT already had a native tool while user-requested TTS did not.
