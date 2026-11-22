# syntax=docker/dockerfile:1
# Toolbox for the PX-CSI migration: ansible-core, the two collections and their Python libraries, pinned, in one image.
# Nothing is installed on the Docker host. This is route A of the README; route B (a plain Ansible machine) uses the same
# two requirements files.
ARG FEDORA_IMAGE=registry.fedoraproject.org/fedora:44
FROM ${FEDORA_IMAGE}
LABEL org.opencontainers.image.title="px-nvme-migration-toolbox"
RUN dnf -y install --setopt=install_weak_deps=False python3 python3-pip ca-certificates tar gzip \
 && dnf clean all
COPY requirements.txt requirements.yml /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt
# Collections go to the system-wide path, never to a home directory.
ENV ANSIBLE_COLLECTIONS_PATH=/usr/share/ansible/collections
RUN ansible-galaxy collection install -r /tmp/requirements.yml -p /usr/share/ansible/collections \
 && rm -rf /root/.ansible
# A passwd entry for the runtime uid; HOME points into the bind-mounted ./.state
ARG TOOLBOX_UID=1000
ARG TOOLBOX_GID=1000
RUN groupadd -g ${TOOLBOX_GID} toolbox \
 && useradd -u ${TOOLBOX_UID} -g ${TOOLBOX_GID} -M -d /work/.state/home -s /bin/bash toolbox
WORKDIR /work
CMD ["bash"]
