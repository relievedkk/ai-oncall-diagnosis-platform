FROM prom/alertmanager:latest

COPY monitoring/alertmanager.yml /etc/alertmanager/alertmanager.yml
