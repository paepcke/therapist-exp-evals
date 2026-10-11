# therapist-exp-evals
Code for all student and therapist trainer evaluations machinery

For details on the cloud machine setup for evaluations, see file
`therapist/system_documentation/vultr_machine_setup.md` in the `therapist` repository.

The agent that pushes the de-identified trainer logs from the trainer's server to the cloud
machine lives in the `therapist` repository (`back_end/src/log_push`, with `log_deid`), because it
runs on that server. This repository holds the code for the cloud machine and for analysis.
