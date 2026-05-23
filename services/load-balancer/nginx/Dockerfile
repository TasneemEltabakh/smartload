FROM nginx:1.25-alpine

COPY nginx.conf /etc/nginx/nginx.conf
COPY conf.d/upstream.conf /etc/nginx/upstream.conf.default
COPY 00-seed-upstream.sh /docker-entrypoint.d/00-seed-upstream.sh
RUN chmod +x /docker-entrypoint.d/00-seed-upstream.sh

EXPOSE 80