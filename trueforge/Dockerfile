# TrueForge, containerised.
#
# Not a preference -- a requirement on Windows. v0.1.4 cannot start natively
# there: it hands an absolute Windows path to the ESM loader without a
# file:// scheme and dies with "Received protocol 'c:'". Its local sandbox
# provider is also macOS/Linux only.
#
# Running it in a Linux container sidesteps both, and has two side benefits:
# the harness lands on the same Docker network as the MCP server, so the
# connector URL is service DNS rather than a host loopback hop; and setup
# becomes one command on every OS instead of three OS-specific paths.
FROM node:22-slim

# git: skills are git repositories, cloned at runtime.
# ca-certificates: outbound TLS to the model provider and Daytona.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @truefoundry/trueforge@0.1.4

# HOST defaults to localhost, which inside a container means the published
# port reaches nothing at all. Bind every interface instead.
ENV HOST=0.0.0.0 \
    PORT=8790 \
    NODE_ENV=production

# env-paths puts SQLite under the home directory; the volume mounted here
# keeps agents, settings and session history across restarts.
VOLUME ["/root/.local/share"]

EXPOSE 8790

CMD ["trueforge", "--port", "8790"]
