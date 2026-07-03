# WisdomInvest web (Next.js 16). Non-standalone on purpose: `next start`
# re-evaluates next.config.ts at boot, so NEXT_PUBLIC_API_BASE set on the
# container drives the /api rewrite at runtime (browser stays same-origin).
FROM node:20-slim

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

ENV NODE_ENV=production
EXPOSE 3000
CMD ["npm", "run", "start"]
