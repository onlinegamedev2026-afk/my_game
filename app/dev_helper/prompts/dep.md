Yes. You have a few good options between “manually SSH into every server” and “full Kubernetes”.

For your stage, I would **not start with Kubernetes**. It is powerful, but it adds a lot: cluster setup, ingress, storage classes, secrets, health probes, Helm, autoscaling, node pools, monitoring, upgrades. For a 10k-user app, Kubernetes can be right later, but it is usually too much for the first production deployment.

**Best Practical Choice: Docker Compose Per Server + Automation**

Even if you do not want “Docker everywhere”, I’d honestly reconsider. Docker Compose is still the easiest way to run this app consistently.

You can use:

```text
Terraform
  creates DigitalOcean servers, firewall, load balancer, DNS records

Ansible
  installs Docker, copies config, creates systemd services, deploys app

Docker Compose
  runs app components on each server
```

That gives you “one command creates infra, one command deploys app” without Kubernetes complexity.

Example workflow:

```bash
terraform apply
ansible-playbook deploy.yml
```

Your final structure becomes:

```text
infra/
  terraform/
    main.tf
    variables.tf
    outputs.tf

deploy/
  ansible/
    inventory.ini
    deploy.yml
    templates/
      app.env.j2
      docker-compose.app.yml.j2
      nginx.conf.j2
```

**Recommended Deployment Style**

Use Docker Compose, but not one giant compose file.

On app servers:

```text
nginx
web app
```

On worker server:

```text
celery worker
```

On scheduler server:

```text
game scheduler
```

Use DigitalOcean managed services for:

```text
PostgreSQL
Redis/Valkey
Load Balancer
```

So you are not running database containers in production.

**Example Server Layout**

```text
App servers:
  docker compose up -d nginx web

Worker server:
  docker compose up -d celery_worker

Scheduler server:
  docker compose up -d game_scheduler

Managed:
  PostgreSQL
  Redis/Valkey
```

This is much easier than installing Python, Gunicorn, Nginx, env files, logs, service units, and dependencies manually on every server.

**If You Want No Docker**

Then use:

```text
Terraform + Ansible + systemd
```

Ansible can do all of this:

- install Python 3.12
- create Linux user
- clone/pull your repo
- create virtualenv
- install `requirements.txt`
- write `.env`
- write Nginx config
- create systemd service for FastAPI
- create systemd service for Celery
- create systemd service for scheduler
- restart services

Your commands would be:

```bash
terraform apply
ansible-playbook deploy.yml
```

This works well, but Docker Compose is cleaner and less fragile.

**My Recommendation**

Use this:

```text
Terraform + Ansible + Docker Compose
```

Not Kubernetes yet.

Why:

- Much simpler than Kubernetes.
- Easy to understand and debug.
- Same behavior on local/staging/production.
- Easy rollback by changing image/tag.
- You can still scale horizontally by adding app servers.
- Later migration to Kubernetes is easier if you already containerized properly.

**When Kubernetes Makes Sense**

Move to Kubernetes only when you have at least 2-3 of these:

- frequent deployments every day
- many services
- autoscaling needs
- multiple developers
- blue/green or canary deployments
- strong uptime/SRE requirements
- need self-healing orchestration across many nodes

For your current app, Kubernetes is probably version 2.

**Simple Production Flow**

Best flow:

```bash
# create/update cloud infra
cd infra/terraform
terraform apply

# deploy app to servers
cd ../deploy/ansible
ansible-playbook deploy.yml

# update app later
ansible-playbook deploy.yml --tags app
```

So yes, you can absolutely have a “command and create stuff” workflow. The tool you are looking for is usually:

```text
Terraform for infrastructure
Ansible for server setup/deployment
Docker Compose for running services
```

That is the sweet spot for your project.