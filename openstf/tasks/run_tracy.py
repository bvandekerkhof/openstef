# SPDX-FileCopyrightText: 2017-2021 Alliander N.V. <korte.termijn.prognoses@alliander.com> # noqa E501>
#
# SPDX-License-Identifier: MPL-2.0

"""run_tracy.py
Tracy checks the mysql todolist and tries her best to execute the
functions with desired inputs
This scripts works as follows:
  1. Checks the mysql table 'todolist' for jobs (which are not already in progress and
    which are not already failed)
  2. Set all newly aquired jobs to 'in progress'
For each job;
  3. Convert input arguments to a dict with 'args' and 'kwargs'
  4. Interpret the given function and arguments
  5. Execute the job
  6. Post result to Slack
  7. Remove job from mysql table
If job fails, set in progress to 2
All functions that tracy is able to execute need to be imported and defined in the
available_functions.

Example:
    This module is meant to be called directly from a CRON job.

    Alternatively this code can be run directly by running::
        $ python run_tracy.py
Attributes:
"""

# sql to create the Tracy jobs table (todolist)

# CREATE TABLE IF NOT EXISTS `tst_icarus`.`todolist` (
# `id` INT NOT NULL AUTO_INCREMENT ,
# `created` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ,
# `function` VARCHAR(200) NOT NULL ,
# `args` VARCHAR(200) NOT NULL ,
# `inprogress` BOOLEAN NULL DEFAULT NULL ,
# PRIMARY KEY (`id`), UNIQUE `id` (`id`))
# ENGINE = InnoDB;


from openstf.monitoring.teams import post_teams
from openstf.tasks.utils.taskcontext import TaskContext
from openstf.tasks.train_model import train_model_task
from openstf.tasks.optimize_hyperparameters import optimize_hyperparameters_task


def run_tracy(context):
    # Get the to do list.
    tracy_jobs = context.database.ktp_api.get_all_tracy_jobs(inprogress=0)
    num_jobs = len(tracy_jobs)

    if num_jobs == 0:
        context.logger.warning("No Tracy jobs to process, exit", num_jobs=num_jobs)
        return

    context.logger.info("Start processing Tracy jobs", num_jobs=num_jobs)

    for i, job in enumerate(tracy_jobs):
        # Set all retrieved items of the todolist to inprogress
        job["inprogress"] = 1
        context.database.ktp_api.update_tracy_job(job)

        context.logger = context.logger.bind(job=job)
        context.logger.info("Process job", job_counter=i, total_jobs=num_jobs)

        # Try to execute Tracy job
        try:
            # If train model job (TODO remove old name when jobs are done)
            if job["function"] in ["train_model", "train_specific_model"]:
                context.logger.info()
                pid = int(job["args"])
                pj = context.database.get_prediction_job(pid)
                train_model_task(pj, context)

            # If optimize hyperparameters job (TODO remove old name when jobs are done)
            elif job["function"] in ["optimize_hyperparameters", "optimize_hyperparameters_for_specific_pid"]:
                pid = int(job["args"])
                pj = context.database.get_prediction_job(pid)
                optimize_hyperparameters_task(pj, context)
            # Else unknown job
            else:
                context.logger.error(f"Unkown Tracy job {job['function']}")
                continue

        # Execution of Tracy job failed
        except Exception as e:
            job["inprogress"] = 2
            context.database.ktp_api.update_tracy_job(job)

            msg = f"Exception occured while processing Tracy job: {job}"
            context.logger.error(msg, exc_info=e)
            post_teams(msg)

        msg = f"Succesfully processed Tracy job: {job}"
        context.logger.info(msg)
        post_teams(msg)

        # Delete job
        context.database.ktp_api.delete_tracy_job(job)
        context.logger.info("Delete Tracy job")

    context.logger = context.logger.unbind("job")
    context.logger.info("Finished processing all Tracy jobs - Tracy out!")


def main():
    with TaskContext("run_tracy") as context:
        run_tracy(context)


if __name__ == "__main__":
    main()
