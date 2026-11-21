FROM nginx:alpine

COPY nginx.conf.template /etc/nginx/conf.d/default.conf
COPY entrypoint.sh /entrypoint.sh
COPY index.html app.js styles.css /usr/share/nginx/html/

RUN chmod +x /entrypoint.sh

EXPOSE 80

ENTRYPOINT ["/entrypoint.sh"]
