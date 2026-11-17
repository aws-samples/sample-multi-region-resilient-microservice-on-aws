FROM public.ecr.aws/amazonlinux/amazonlinux:2023.12.20261112.0

# We tell DNF not to install Recommends and Suggests packages, which are
# weak dependencies in DNF terminology, thus keeping our installed set of
# packages as minimal as possible.
#
# Amazon Linux 2023 repositories are versioned: a plain dnf upgrade only reaches the release in the
# image tag. --releasever=latest takes the newest security updates when the image is built, and
# dnf clean all drops the repository metadata that adds to the layer.
RUN dnf --setopt=install_weak_deps=False -y upgrade --releasever=latest && \
    dnf --setopt=install_weak_deps=False -y install nginx && \
    dnf clean all

EXPOSE 8080

COPY default.conf /etc/nginx/nginx.conf
COPY mime.types /etc/nginx/mime.types
COPY public/ /usr/share/nginx/html

RUN chown -R nginx:nginx /var/log/nginx && \
        chown -R nginx:nginx /etc/nginx/conf.d && \
        chown -R nginx:nginx /usr/share/nginx/html
RUN touch /var/run/nginx.pid && \
        chown -R nginx:nginx /var/run/nginx.pid

USER nginx
HEALTHCHECK CMD curl --fail http://localhost:8080 || exit 1
STOPSIGNAL SIGQUIT

CMD ["nginx", "-g", "daemon off;"]
