# Optional, disposable CLI acceptance runner. Not a Compose service or runtime.
FROM node:24-alpine
RUN npm install -g @openai/codex@0.159.2 --no-audit --no-fund
WORKDIR /qa
