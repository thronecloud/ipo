# WisdomInvest web (Next.js 16).
#
# The /api rewrite destination is BAKED AT BUILD TIME: `next build` resolves
# next.config.ts rewrites() into .next/routes-manifest.json, and `next start`
# serves that frozen manifest without re-invoking rewrites(). So the API base
# must be present as a build ARG — a runtime-only env var arrives too late and
# the localhost fallback gets baked instead (this was ISSUE-001).
FROM node:20-slim

WORKDIR /app

# Backend base for the /api proxy + RSC fetches. Defaults to the compose
# network address; override via docker-compose build.args for other hosts.
ARG NEXT_PUBLIC_API_BASE=http://api:8000
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

ENV NODE_ENV=production
EXPOSE 3000
CMD ["npm", "run", "start"]
