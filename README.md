# therapist-exp-evals
Code for all student and therapist trainer evaluations machinery

For details on the cloud machine setup for evaluations, see file
`therapist/system_documentation/vultr_machine_setup.md` in the `therapist` repository.

The agent that pushes the de-identified trainer logs from the trainer's server to the cloud
machine lives in the `therapist` repository (`back_end/src/log_push`, with `log_deid`), because it
runs on that server. This repository holds the code for the cloud machine and for analysis.

Contents (under `src/`):

* `log_analysis`: reads the de-identified trainer logs and builds the usage tables and reports.
* `log_dashboard`: the read-only Tornado usage dashboard on the cloud machine. Install steps are in
  `deploy/logdash_install.md`; the systemd unit and Caddy block are in `deploy/`.
* `evals_service`: the evaluation web service: instructors' unblinded assessments of students,
  students' self-assessments, and blinded evaluations of student sessions with the trainer. Install
  steps are in `deploy/evals_install.md`. The sessions it shows come from the bundle builder in the
  `therapist` repository (`back_end/src/student_eval/bundle_builder.py`).
* `backup_pull`: runs on the administrator's laptop. Pulls the evaluation snapshots from the cloud machine and the
  pseudonym secret from the trainer's server every hour, verifies them, and sends a notification and an e-mail
  when a backup is more than 48 hours old. Install steps: `deploy/backup_install.md`.
