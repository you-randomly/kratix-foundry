FROM node:20-alpine

WORKDIR /app

# Install a simple HTTP server with API proxy capability
RUN npm install -g http-server

# Copy static and server files
COPY index.html styles.css app.js server.js ./

EXPOSE 8080

CMD ["node", "server.js"]
