FROM node:20-alpine

WORKDIR /app

# Install a simple HTTP server with API proxy capability
RUN npm install -g http-server

# Copy static files
COPY index.html styles.css app.js ./

# The API endpoints will be handled by a sidecar or ingress-level routing
# This container just serves the static files

EXPOSE 8080

CMD ["http-server", "-p", "8080", "-c-1"]
