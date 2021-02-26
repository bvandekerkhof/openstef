# SPDX-FileCopyrightText: 2017-2021 Alliander N.V. <korte.termijn.prognoses@alliander.com> # noqa E501>
#
# SPDX-License-Identifier: MPL-2.0

from datetime import datetime

import numpy as np
import pandas as pd
from ktpbase.database import DataBase
from ktpbase.log import logging
import scipy.optimize

import openstf.monitoring.teams as monitoring

COEF_MAX_PCT_DIFF = 0.3


def split_energy(pid):
    """Function that caries out the energy splitting for a specific prediction job with id pid

    Args:
        pid (int): Prediction job id

    Returns:
        pandas.DataFrame: Energy splitting coefficients.
    """
    # Make database connection
    db = DataBase()
    logger = logging.get_logger(__name__)

    # Get Prediction job
    pj = db.get_prediction_job(pid)

    logger.info("Start splitting energy", pid=pj["id"])

    # Get input for splitting
    input_split_function = db.get_input_energy_splitting(pj)

    # Carry out the splitting
    components, coefdict = find_components(input_split_function)

    # Calculate mean absolute error (MAE)
    # TODO: use a standard metric function for this
    error = components[["load", "Inschatting"]].diff(axis=1).iloc[:, 1]
    mae = error.abs().mean()
    coefdict.update({"MAE": mae})
    coefsdf = convert_coefdict_to_coefsdf(pj, input_split_function, coefdict)

    # Get average coefs of previous runs and check if new coefs are valid
    mean_coefs = db.get_energy_split_coefs(pj, mean=True)
    if are_new_coefs_valid(coefdict, mean_coefs) is False:
        # If coefs not valid, do not update the coefs in the db and send teams
        # message that something strange is happening
        monitoring.post_teams_alert(
            "New splitting coefficients for pid {} deviate strongly from previously stored coefficients.".format(
                pj["id"],
            ),
            coefsdf=coefsdf,
        )
        # Use the last known coefficients for further processing
        last_coefdict = db.get_energy_split_coefs(pj)
        last_coefsdf = convert_coefdict_to_coefsdf(
            pj, input_split_function, last_coefdict
        )
        return last_coefsdf

    # Save Results
    db.write_energy_splitting_coefficients(coefsdf, if_exists="append")
    logger.info("Succesfully wrote energy split coefficients to database", pid=pj["id"])
    return coefsdf


def are_new_coefs_valid(new_coefs, mean_coefs):
    """Check if new coefficients are valid.

    Args:
        new_coefs (dict): new coefficients for standard load profiles
        mean_coefs (dict): average of previous coefficients for standard load profiles

    Returns:
        boolean: bool whether new coefs are valid
    """
    # Loop over keys and check if the absolute difference with the average value is not
    # more than COEF_MAX_PCT_DIFF x absolute average value.
    # If no previous coefs are stored an mean_coefs is empty and this loop wil not run
    for key in mean_coefs.keys():
        diff = np.abs(mean_coefs[key] - new_coefs[key])
        if diff > COEF_MAX_PCT_DIFF * np.abs(mean_coefs[key]):
            return False
    return True


def convert_coefdict_to_coefsdf(pj, input_split_function, coefdict):
    """Convert dictionary of coefficients to dataframe with additional data for db storage.

    Args:
        pj (PredictionJob): prediction job
        input_split_function (pd.DataFrame): df of columns of standard load profiles,
            i.e. wind, solar, household
        coefdict (dict): dict of coefficient per standard load profile

    Returns:
        pd.DataFrame: df of coefficients to insert in sql
    """
    #
    sql_column_labels = ["pid", "date_start", "date_end", "created"]
    sql_colum_values = [
        pj["id"],
        input_split_function.index.min().date(),
        input_split_function.index.max().date(),
        datetime.utcnow(),
    ]
    coefsdf = pd.DataFrame(
        {"coef_name": list(coefdict.keys()), "coef_value": list(coefdict.values())}
    )
    for i, column in enumerate(sql_column_labels):
        coefsdf[column] = sql_colum_values[i]

    return coefsdf


def find_components(df, zero_bound=True):
    """Function that does the actual energy splitting

    Args:
        df (pandas.DataFrame): Input data. The dataframe should contain these columns
            in exactly this order: [load, wind_ref, pv_ref, mulitple tdcv colums]
        zero_bound (bool): If zero_bound is True coefficients can't be negative.

    Returns:
        tuple:
            [0] pandas.DataFrame: Containing the wind and solar components
            [1] dict: The coefficients that result from the fitting
    """

    # Define function to fit
    def weighted_sum(x, *args):
        if len(x) != len(args):
            raise Exception("Length of args should match len of x")
        weights = np.array([v for v in args])
        return np.dot(x.T, weights)

    load = df.iloc[:, 0]
    wind_ref = df.iloc[:, 1]
    pv_ref = df.iloc[:, 2]

    # Define scaler
    nedu_scaler = (load.max() - load.min()) / 10

    # Come up with inital guess for the fitting
    p_wind_guess = 1.0
    ppv_guess = 1.0
    p0 = [p_wind_guess, ppv_guess] + (len(df.columns) - 3) * [nedu_scaler]

    # Define fitting bounds
    if zero_bound:
        bounds = (0, "inf")
    else:
        bounds = ("-inf", "inf")

    # Carry out fitting
    # See https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.curve_fit.html # noqa
    coefs, cov = scipy.optimize.curve_fit(
        weighted_sum,
        xdata=df.iloc[:, 1:].values.T,
        ydata=load.values,
        p0=p0,
        bounds=bounds,
        method="trf",
    )

    # Set 'almost zero' to zero
    coefs[coefs < 0.1] = 0

    # Reconstuct historical load
    hist = weighted_sum(df.iloc[:, 1:].values.T, *coefs)
    histp0 = weighted_sum(df.iloc[:, 1:].values.T, *p0)

    # Make a nice dataframe to return the components
    components = df.iloc[:, [0]].copy()
    components["Inschatting"] = hist.T
    components["p0"] = histp0.T
    components["Windopwek"] = wind_ref * coefs[0]
    components["Zonne-opwek"] = pv_ref * coefs[1]
    components["StandaardVerbruik"] = (df.iloc[:, 3:] * coefs[2:]).sum(axis=1)
    components["Residu"] = -1 * components.iloc[:, 0:2].diff(axis=1).iloc[:, 1]

    # Make nice dictinary to return coefficents
    coefdict = {name: value for name, value in zip(df.columns[1:], coefs)}

    # Return result
    return components, coefdict
