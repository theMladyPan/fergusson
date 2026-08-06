# GWS credential isolation

Fergusson now loads Chirp’s service-account key explicitly from `STT_CREDENTIALS_FILE`, preventing `gws` subprocesses from inheriting that identity through process-wide ADC. Google Workspace authentication troubleshooting now routes through the tracked `gws-debug` skill, which enforces user OAuth for consumer Gmail and provides a headless OAuth/tunnel helper. This fixes the real Odroid failure where `gws` silently fell back to the Chirp service account and returned a misleading Cloud IAM 403.
