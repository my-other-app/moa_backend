# moa-render: the render service for the ticket, certificate and ID card
# designs (app/core/doc_designs; doc-designs contract D1). Gotenberg 8's
# Chromium-only image, pinned by tag and digest (8.39.0-chromium, pushed
# 2026-10-27), with every font the designs use installed system-wide.
#
# Build from the repository root (the context is backend/, for the fonts):
#
#     docker build -f backend/render/Dockerfile -t moa-render:8.39.0-1 backend
#
# The pages the backend sends carry their fonts and pictures inline (data:
# URIs), so a render needs nothing from here but Chromium; the fonts below
# are the same static faces as the pages' (assets/doc-fonts/fonts.lock.json),
# installed so a family a stylesheet names always resolves here too.
# The flags (time limits, queue, restarts, what Chromium may load) are in
# docker-compose.prod.yml and ops/server/compose.yml, where they can change
# without a rebuild. docs/OPERATIONS.md, "The render service".
FROM gotenberg/gotenberg:8.39.0-chromium@sha256:558943677cf9092df83ff654c2645856dcb9ddf777f359243789688e74f3bcdd

USER root
COPY assets/doc-fonts/ttf/ /usr/local/share/fonts/moa-doc/
COPY assets/doc-fonts/licenses/ /usr/local/share/fonts/moa-doc/licenses/
# Fail the build if fontconfig does not know the families by their CSS names.
RUN chmod -R a+rX /usr/local/share/fonts/moa-doc \
    && fc-cache -f \
    && for family in "Barlow Condensed" "Fraunces" "IBM Plex Mono" "Instrument Serif" "Space Grotesk" "Saira Condensed"; do \
         fc-list : family | grep -q "$family" || { echo "fontconfig does not know $family"; exit 1; }; \
       done

USER gotenberg
