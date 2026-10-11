# Installing the usage dashboard on the Vultr machine

Run as a sudo-capable user. Assumes the clone is in `/opt/therapist-exp-evals`
and the logs arrive in `/srv/thdata/inbox/logs` (see `vultr_machine_setup.md`
in the `therapist` repo).

1. Clone, conda env and install. Do the clone as your own user, not with `sudo`:

    root has no GitHub key (git under `sudo` looks in `/root/.ssh`). Only creating the
    directory needs root. Add the machine's `~/.ssh/id_ed25519.pub` to GitHub first,
    preferably as a read-only deploy key on the repo.

        sudo install -d -o $USER -g $USER /opt/therapist-exp-evals
        git clone git@github.com:<you>/therapist-exp-evals.git /opt/therapist-exp-evals
        conda create -n therapist-exp-evals python=3.11 && conda activate therapist-exp-evals
        cd /opt/therapist-exp-evals && pip install -e .

    The `logdash` service account only reads the code and the env, so both must be
    world-readable (`/opt/therapist-exp-evals` and the env's directory; check with
    `sudo -u logdash /path/to/env/bin/python -c "import log_dashboard"`).
    Adjust `ExecStart` in the unit if the env lives elsewhere than `/opt/conda/envs/therapist-exp-evals`.

2. Account and password:

        sudo useradd --system --home /srv/logdash --shell /usr/sbin/nologin logdash
        sudo install -d -o logdash -g logdash -m 750 /srv/logdash
        sudo -u logdash /opt/conda/envs/therapist-exp-evals/bin/python -m log_dashboard.password_file --file /srv/logdash/auth.txt --user <name>

    The `logdash` account must be able to read `/srv/thdata/inbox/logs` (group read; it never writes there).

3. Service:

        sudo cp deploy/thtrainer-logdash.service /etc/systemd/system/
        sudo systemctl daemon-reload && sudo systemctl enable --now thtrainer-logdash
        curl -s http://127.0.0.1:8081/healthz

4. TLS: append `deploy/Caddyfile.logdash` to `/etc/caddy/Caddyfile` (keep the existing sites), validate, reload.

    The DNS name is an sslip.io subdomain, so no DNS setup is needed.

5. Browse to https://dash.149-28-212-36.sslip.io/ and log in. The page shows nothing until the first push has arrived.

The dashboard refuses to serve logs containing '@' (un-de-identified data).
