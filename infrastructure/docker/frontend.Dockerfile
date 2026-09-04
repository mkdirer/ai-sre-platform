# syntax=docker/dockerfile:1.7

FROM node:24-alpine AS build
WORKDIR /app
# VITE_* values are baked at build time; accept the demo actor as a build arg
# so the bundle cannot silently diverge from .env.
ARG VITE_APPROVAL_ACTOR=local-demo-approver
ENV VITE_APPROVAL_ACTOR=$VITE_APPROVAL_ACTOR
COPY apps/frontend/package.json apps/frontend/package-lock.json* ./
RUN npm ci
COPY apps/frontend ./
RUN npm run build

FROM nginx:1.27-alpine AS runtime
COPY infrastructure/docker/frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
