# Build stage for dependencies
FROM node:22-alpine AS deps

WORKDIR /app
ENV NODE_ENV=production

# Copy package.json to install dependencies
COPY package.json ./
# Install only production dependencies and clean cache
RUN npm install lighthouse@latest --omit=dev && npm cache clean --force

# Final runtime stage
FROM node:22-alpine

# Set environment variables
ENV NODE_ENV=production \
    PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium-browser

# Install Chromium and necessary fonts
# Combined into a single RUN instruction to minimize layers
RUN apk add --no-cache \
    chromium \
    nss \
    freetype \
    harfbuzz \
    ca-certificates \
    ttf-freefont

WORKDIR /app

# Copy node_modules from deps stage
COPY --from=deps /app/node_modules ./node_modules
COPY package.json ./

# Copy application source
COPY lcp.js ./
COPY urls.txt ./

# Create output directory
RUN mkdir -p lcp_output

# Set the entrypoint
ENTRYPOINT ["node", "lcp.js"]
CMD []
