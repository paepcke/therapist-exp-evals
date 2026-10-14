# Installing the evaluation service on the Vultr machine

Run as a sudo-capable user. Assumes the repo is cloned in `/opt/therapist-exp-evals`
with its conda env `therapist-exp-evals` (see `logdash_install.md`, step 1), and
that `git pull` plus `pip install -e .` have brought in the latest code. The
service lives at `https://rate.149-28-212-36.sslip.io/`.

1. Account and directories. The `evals` account is only for this service:

        id evals || sudo useradd --system --home /srv/evals --shell /usr/sbin/nologin evals
        sudo install -d -o evals -g evals -m 750 /srv/evals /srv/evals/config /srv/evals/data

    The service must be able to read the bundles that arrive from quatro. Check with
    the next line; it must list the directory without an error. If it does not,
    give the `evals` account group read access to `/srv/thdata/inbox/evals`.

        sudo -u evals ls /srv/thdata/inbox/evals

2. Check that the service account can run the code:

        sudo -u evals /opt/conda/envs/therapist-exp-evals/bin/python -c "import evals_service"

3. The roster: who the instructors and students are, and when each milestone opens.
   Copy `deploy/roster.example.json`, edit it, and install it:

        sudo install -o evals -g evals -m 640 roster.json /srv/evals/config/roster.json

    Instructors become evaluators. Students get a login so that they can fill in their
    self-assessments. Their e-mail addresses are therefore stored on this machine,
    in the roster and the database, and nowhere else in the pipeline. The service
    picks up roster changes without a restart.

4. Create the accounts. First yourself, as administrator and evaluator, and Jon
   as Evaluator_1. Each command prints an invite code, shown only once:

        sudo -u evals /opt/conda/envs/therapist-exp-evals/bin/python -m evals_service.evals_admin invite --email YOU@example.org --roles admin,evaluator --name "Andreas Paepcke"
        sudo -u evals /opt/conda/envs/therapist-exp-evals/bin/python -m evals_service.evals_admin invite --email jon@example.org --roles evaluator --name "Jon Frederickson" --label Evaluator_1

    Then everybody else from the roster at once (prints a CSV with their codes):

        sudo -u evals /opt/conda/envs/therapist-exp-evals/bin/python -m evals_service.evals_admin import-roster

    Give each person their code over a channel you trust. They open the site, choose
    "Use your invite code", enter their e-mail address, the code and a password of
    their own (at least 10 characters). After that, their browser remembers them.
    `list` shows who has activated; `invite --reissue` gives a new code; `disable`
    locks an account.

5. Service:

        sudo cp deploy/thtrainer-evals.service /etc/systemd/system/
        sudo systemctl daemon-reload && sudo systemctl enable --now thtrainer-evals
        curl -s http://127.0.0.1:8082/healthz

    The last line must print `ok`. Otherwise: `journalctl -u thtrainer-evals -n 30`.

6. TLS: append `deploy/Caddyfile.evals` to `/etc/caddy/Caddyfile` (keep the existing
   sites), validate, reload:

        sudo caddy validate --config /etc/caddy/Caddyfile
        sudo systemctl reload caddy

7. Browse to https://rate.149-28-212-36.sslip.io/. Session reviews appear once a
   bundle has been pushed from quatro into `/srv/thdata/inbox/evals`; assessments
   appear when a milestone's `opens` date has come.

The administrator's page ("Progress") lists every evaluator and student with e-mail
address, what is done, what is outstanding and the days since last activity; it
filters for people who need a reminder, copies their addresses, and downloads
all submitted ratings as CSV.

Back up `/srv/evals/data` (the SQLite database) regularly once ratings exist. It
is the only copy of the ratings:

    sudo -u evals sqlite3 /srv/evals/data/evals.sqlite ".backup '/srv/evals/data/backup.sqlite'"
