# Backups of the evaluation data and the pseudonym secret

What is protected, and where the copies end up:

* The evaluation database on the cloud machine (the only copy of the human ratings), as
  nightly snapshots plus a CSV of the submitted ratings.
* On quatro: `~/.ssh/eval_pseudonym_secret.txt` (without it, a student gets a different
  code next time) and `~/eval_state/sampling_state.json` (which cases the evaluators were
  given). Quatro itself is not backed up.

Sparky pulls all of this every hour into `~/Backups/thtrainer/`, where Time Machine and the AWS
bucket pick it up. Sparky also checks every pull. When a source's newest data is more than
48 hours old, it shows a macOS notification and sends a push message to your phone (through
ntfy.sh), again every 24 hours until it is fixed. E-mail is possible but optional. Away from Stanford, quatro is reachable only through the VPN: start it,
and the next hourly run (or `--force`) catches up.

Important: both the ratings and the secret can identify people. The Time Machine disk and the
AWS bucket must be encrypted (FileVault on sparky; encryption of the Time Machine backup;
server-side encryption on the bucket). Check this before the first real ratings exist.

## Part A: cloud machine

Commands run as the administrator, with `sudo`.

1. Update the code, then create the account that sparky pulls with, and the snapshot directory.
   The account can do exactly one thing: read that directory (step 3).
   
        cd /opt/therapist-exp-evals && git pull && /opt/conda/envs/therapist-exp-evals/bin/pip install -e .
        sudo useradd --system --user-group --create-home --home-dir /home/evpull --shell /bin/bash evpull
        sudo usermod -p '*' evpull
        sudo install -d -o evals -g evpull -m 2750 /srv/evals-backup

2. Allow the account to log in over SSH. Edit `/etc/ssh/sshd_config.d/10-thtrainer.conf`
   so that the `AllowUsers` line also names it, then check and reload:
   
        grep AllowUsers /etc/ssh/sshd_config.d/10-thtrainer.conf
        # must read: AllowUsers paepcke ingest evpull
        sudo sshd -t && sudo systemctl reload ssh

3. Make the key on **sparky** (not here), and print the public key:
   
        ssh-keygen -t ed25519 -f ~/.ssh/vultr_evals_backup -N '' -C sparky-evals-backup
        cat ~/.ssh/vultr_evals_backup.pub
   
    Back on the cloud machine, paste the whole line into `PUBKEY`. The check line must not
    complain.
   
        PUBKEY='<paste the whole line here>'
        [[ "$PUBKEY" == ssh-ed25519\ AAAA* ]] || echo "ERROR: PUBKEY is not a real key line"
        sudo install -d -o evpull -g evpull -m 700 /home/evpull/.ssh
        printf 'restrict,command="/usr/bin/rrsync -ro /srv/evals-backup" %s\n' "$PUBKEY" \
          | sudo -u evpull tee /home/evpull/.ssh/authorized_keys >/dev/null
        sudo chmod 600 /home/evpull/.ssh/authorized_keys
        sudo ssh-keygen -lf /home/evpull/.ssh/authorized_keys

4. The nightly snapshot:
   
        sudo cp deploy/thtrainer-evals-backup.service deploy/thtrainer-evals-backup.timer /etc/systemd/system/
        sudo systemctl daemon-reload && sudo systemctl enable --now thtrainer-evals-backup.timer
        sudo systemctl start thtrainer-evals-backup.service
        journalctl -u thtrainer-evals-backup -n 5 --no-pager
        sudo ls -l /srv/evals-backup
   
    The journal must say `Backup evals-...sqlite: N people, N submitted ratings`. The listing
    shows the snapshot, the CSV, `roster.json` and `LATEST.json`, owned by `evals`, group
    `evpull`, mode `rw-r-----`. `systemctl list-timers thtrainer-evals-backup.timer` shows the
    next run (03:30).

## Part B: sparky

First, a newer rsync. The rsync that macOS ships is `openrsync`, and the cloud machine's restricted
upload account rejects it ("invalid rsync-command syntax or options"). Install rsync 3.x and check it;
the first line must say `rsync  version 3.`:

    brew install rsync
    /opt/homebrew/bin/rsync --version | head -1

The example configuration below names `/opt/homebrew/bin/rsync` (`"rsync"` setting). On an
Intel Mac, brew installs to `/usr/local/bin/rsync`: change the setting then. Use it for the
pull test in step 3 of Part A as well:

    rsync_path=/opt/homebrew/bin/rsync
    mkdir -p /tmp/pulltest && $rsync_path -rtv -e "ssh -i ~/.ssh/vultr_evals_backup -o IdentitiesOnly=yes" evpull@149-28-212-36.sslip.io:/ /tmp/pulltest/

1. The configuration. Copy the example, then edit nothing unless your paths differ. It names
   the two sources and the phone topic. The code is the `backup_pull` package of this repository; the
   conda environment has it once the repository is installed in it (`pip install -e .`).
   
        cd ~/VSCodeWorkspaces/therapist-exp-evals && conda activate therapist-exp-evals && pip install -e .
        mkdir -p ~/.thtrainer ~/Backups/thtrainer
        cp deploy/backup_pull.example.json ~/.thtrainer/backup_pull.json

2. The phone. Reminders go to your phone through ntfy.sh, a free push service: you install its
   app, subscribe to one secret topic name, and sparky posts to that name. Anybody who knows the
   name can read the messages, so it must be long and random. The messages say only what is
   stale and what to do, nothing about hosts or data. Make the name:
   
        python -c "import secrets; print('thtrainer-backup-' + secrets.token_urlsafe(18))"
   
    Put the result in `~/.thtrainer/backup_pull.json`, in place of `CHOOSE-A-LONG-RANDOM-NAME`
    in the `ntfy` block. On the phone, install the **ntfy** app (App Store or Google Play),
    tap "+" and subscribe to exactly that topic name on the default server (ntfy.sh). Allow
    notifications for the app.

3. Test that reminders reach you. A notification must appear on sparky and a message
   "[thtrainer backup] test" on the phone. The command prints one line per channel:
   
        conda activate therapist-exp-evals
        python -m backup_pull.backup_puller --test-alert

    To get e-mail too, add a `mail` block to the configuration (`user`, `to`, and optionally
    `smtp_host`, `smtp_port`, `keychain_service`), and store the SMTP password in the Keychain
    with `security add-generic-password -a <user> -s thtrainer-backup-smtp -T /usr/bin/security -w`.
    Gmail did not work from this laptop (it hung up when the login arrived), which is why push
    is the default.

4. A first pull by hand, on the Stanford network or VPN (quatro is needed):
   
        python -m backup_pull.backup_puller --force
   
    The sampling-state file exists on quatro only after the first bundle has been built. Until
    then, delete its line from the `quatro-secrets` remotes in your configuration (and the comma
    after the line above it), and put it back after the first bundle.
   
    The report shows `newest data 0 hours old` for both sources. Look at
    `~/Backups/thtrainer`: `vultr-evals` has the snapshots, `quatro-secrets` the two files.
    If a source fails, the report says why. The usual causes: the SSH key for
    `thtrainer@quatro` needs its passphrase (add the key to the macOS agent with
    `ssh-add --apple-use-keychain ~/.ssh/<key>`, since a background job cannot type one), or
    you are off the VPN.

5. The hourly job:
   
        mkdir -p ~/Library/LaunchAgents
        cp deploy/com.thtrainer.backup-pull.plist ~/Library/LaunchAgents/
        launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.thtrainer.backup-pull.plist
        launchctl print gui/$(id -u)/com.thtrainer.backup-pull | grep -E 'state|last exit'

    Check the python path in the plist if the job reports a failure to start. To see what the
    job did: `tail ~/Backups/thtrainer/backup_pull.log`, and `python -m backup_pull.backup_puller
    --status` for the state of each source. To stop it: `launchctl bootout
    gui/$(id -u)/com.thtrainer.backup-pull`.

## What the reminders mean

* **quatro-secrets not backed up for N days**: quatro could not be reached. At home, start the VPN.
  The next hourly run succeeds; you need do nothing else. A password-protected key or an
  unreachable quatro show up in the message.
* **vultr-evals not backed up for N days, last attempt failed**: the cloud machine could not be
  reached or the snapshot failed its check (checksum or SQLite integrity). Look at the message.
* **vultr-evals ... the last pull worked, but the data is old**: the nightly snapshot on the cloud
  machine stopped. Run `systemctl status thtrainer-evals-backup.service` there.
* **never been backed up**: the first pull has not succeeded yet.

If sparky is asleep or off, nothing runs and nothing is sent (the phone message comes from sparky too). The first run after it wakes does the
catch-up. Over a long trip, that is the gap the 48 hours cannot cover.

## Restoring

Needs a snapshot (`evals-<stamp>.sqlite`) and, for a new machine, the roster. The newest is named in
`LATEST.json`. On the (new) cloud machine, after installing the service as in `evals_install.md`
but before anybody uses it:

1. Stop the service and put the files in place:
   
        sudo systemctl stop thtrainer-evals
        sudo install -o evals -g evals -m 640 evals-<stamp>.sqlite /srv/evals/data/evals.sqlite
        sudo rm -f /srv/evals/data/evals.sqlite-wal /srv/evals/data/evals.sqlite-shm
        sudo install -o evals -g evals -m 640 roster.json /srv/evals/config/roster.json

2. Start it and look at the progress page as the administrator: everybody and their ratings
   must be there. Evaluators' browsers stay logged in, since the device tokens are in the
   database.
   
        sudo systemctl start thtrainer-evals
        curl -s http://127.0.0.1:8082/healthz

The CSV (`ratings-<stamp>.csv`) is for analysis and needs no restore. To restore the quatro files,
copy them back to `~/.ssh/eval_pseudonym_secret.txt` (mode 600) and `~/eval_state/`.
