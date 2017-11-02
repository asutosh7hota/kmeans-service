"""
Misc. utility functions for formatting, data wrangling, and plotting.

Author: Angad Gill
"""
import io
import os
import random
import time
import base64
import urllib.parse

import boto
import boto3

import pandas as pd
import numpy as np
import seaborn as sns
from matplotlib import pyplot as plt
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas

from flask import make_response
from config import UPLOAD_FOLDER, ALLOWED_EXTENSIONS, SPATIAL_COLUMNS, EXCLUDE_COLUMNS
from config import S3_BUCKET, EUCA_S3_HOST, EUCA_S3_PATH, EUCA_KEY_ID, EUCA_SECRET_KEY
from database import mongo_get_job, mongo_get_tasks

def format_date_time(epoch_time):
    """
    Converts epoch time string to (Date, Time) formatted as ('04 April 2017', '11:01 AM').

    Parameters
    ----------
    epoch_time: str
        Epoch time converted to str.

    Returns
    -------
    (str, str)
        (Date, Time) formatted as ('04 April 2017', '11:01 AM')
    """
    start_time = time.localtime(float(epoch_time))
    start_time_date = time.strftime("%d %B %Y", start_time)
    start_time_clock = time.strftime("%I:%M %p", start_time)
    return start_time_date, start_time_clock


def float_to_str(num):
    """
    Convert float to str with 4 decimal places.

    Parameters
    ----------
    num: float

    Returns
    -------
    str

    """
    return '{:.4f}'.format(num)


""" Data wrangling functions """


def filter_dict_list_by_keys(dict_list, keys):
    """
    Keep only keys specified in the function parameter `keys`. Does not modify dicts in `dict_list`.

    Parameters
    ----------
    dict_list: list(dict)
    keys: list(str)

    Returns
    -------
    list(dict)

    """
    new_dict_list = []
    for d in dict_list:
        new_d = {}
        for k, v in d.items():
            if k in keys:
                new_d[k] = v
        new_dict_list += [new_d]
    return new_dict_list


def tasks_to_best_results(tasks):
    """
    Finds the best clustering among tasks for each covar_type-covar_tied pair.

    Method returns the best values for k, labels, and BIC for all covar_type
    and covar_tied. 'Best' corresponds to highest BIC value.

    Parameters
    ----------
    tasks: list(dict)

    Returns
    -------
    list(str), list(bool), list(int), list(list(int)), list(float)
        list of covar_type strings
        list of covar_tied bools
        list of k values
        list of labels
        list of BIC values
        list of task_id
    """
    # Filter list of dicts to reduce the size of Pandas DataFrame
    df = pd.DataFrame(filter_dict_list_by_keys(tasks, ['task_id', 'k', 'covar_type', 'covar_tied', 'bic', '_id']))

    # Subset df to needed columns and fix types
    df['bic'] = df['bic'].astype('float')
    df['k'] = df['k'].astype('int')

    # For each covar_type and covar_tied, find k that has maximum bic score
    df = df.loc[df.groupby(['covar_type', 'covar_tied', 'k'])['bic'].idxmax()]
    df = df.loc[df.groupby(['covar_type', 'covar_tied'])['bic'].idxmax()]

    labels = []
    for row in df['_id']:
        labels += [t['labels'] for t in tasks if t['_id'] == row]
    df.reset_index(drop=True, inplace=True)
    return df['covar_type'].tolist(), df['covar_tied'].tolist(),df['k'].tolist(), labels, df['bic'], df['task_id'].tolist()


def tasks_to_best_task(tasks):
    """
    Finds the single best cluster assignment corresponding to the highest BIC.

    Parameters
    ----------
    tasks: list(dict)

    Returns
    -------
    k: int - number of clusters
    labels: list(int) - cluster assignments
    task_id: int - task_id of the best task
    """
    # Filter list of dicts to reduce the size of Pandas DataFrame
    _, _, ks, labels, bics, task_ids = tasks_to_best_results(tasks)
    index = np.argmax(np.array(bics))
    return ks[index], bics[index], labels[index], task_ids[index]


def task_stats(n_tasks, tasks):
    """
    Find statistics on tasks statuses.

    Parameters
    ----------
    n_tasks: int
        Total number of tasks for a job.
    tasks: list(dict)
        List of task objects from the database for the job.

    Returns
    -------
    dict
        Contains:
            n_tasks: total number of tasks for a job. Sames as the function parameter
            n_tasks_done: number of tasks marked as 'done'
            per_done: percentage of all tasks marked as 'done'
            n_tasks_pending: number of tasks marked as 'pending'
            per_pending: percentage of all tasks marked as 'pending'
            n_tasks_error: number of tasks marked as 'error'
            per_error: percentage of all tasks marked as 'error'
            n_tasks_submitted: number of tasks entered in the database for the job
            per_submitted: percentage of all tasks that are in the database for the job

    """
    n_tasks_submitted = len(tasks)
    per_submitted = '{:.0f}'.format(n_tasks_submitted / n_tasks * 100)
    n_tasks_done, n_tasks_pending, n_tasks_error = 0, 0, 0

    if n_tasks_submitted > 0:
        n_tasks_done = len([x for x in tasks if x['task_status'] == 'done'])
        n_tasks_pending = len([x for x in tasks if x['task_status'] == 'pending'])
        n_tasks_error = len([x for x in tasks if x['task_status'] == 'error'])

    per_done = '{:.1f}'.format(n_tasks_done / n_tasks * 100)
    per_pending = '{:.1f}'.format(n_tasks_pending / n_tasks * 100)
    per_error = '{:.1f}'.format(n_tasks_error / n_tasks * 100)

    stats = dict(n_tasks=n_tasks,
                 n_tasks_done=n_tasks_done, per_done=per_done,
                 n_tasks_pending=n_tasks_pending, per_pending=per_pending,
                 n_tasks_error=n_tasks_error, per_error=per_error,
                 n_tasks_submitted=n_tasks_submitted, per_submitted=per_submitted)
    return stats


def filter_by_min_members(tasks, min_members=10):
    """
    Keep tasks only if they have at least `min_members` points in each cluster. Does not modify `tasks`.

    Parameters
    ----------
    tasks: list(dict)
        List of task objects for a job.
    min_members: int

    Returns
    -------
    list(dict)

    """
    filtered_tasks = []
    for task in tasks:
        if np.all(np.bincount(task['labels']) > min_members):
            filtered_tasks += [task]
    return filtered_tasks


""" Plotting functions  """


def plot_aic_bic_fig(tasks):
    """
    Creates AIC-BIC plot, as a 2-row x 3-col grid of point plots with 95% confidence intervals.

    Parameters
    ----------
    tasks: list(dict)

    Returns
    -------
    Matplotlib Figure object
    """
    sns.set(context='talk', style='whitegrid')
    # Filter list of dicts to reduce the size of Pandas DataFrame
    df = pd.DataFrame(filter_dict_list_by_keys(tasks, ['k', 'covar_type', 'covar_tied', 'bic', 'aic']))
    df['covar_type'] = [x.capitalize() for x in df['covar_type']]
    df['covar_tied'] = [['Untied', 'Tied'][x] for x in df['covar_tied']]
    df['aic'] = df['aic'].astype('float')
    df['bic'] = df['bic'].astype('float')
    df = pd.melt(df, id_vars=['k', 'covar_type', 'covar_tied'], value_vars=['aic', 'bic'], var_name='metric')
    f = sns.factorplot(x='k', y='value', col='covar_type', row='covar_tied', hue='metric', data=df,
                       row_order=['Tied', 'Untied'], col_order=['Full', 'Diag', 'Spher'], legend=True, legend_out=True,
                       ci=95, n_boot=100)
    f.set_titles("{col_name}-{row_name}")
    f.set_xlabels("Num. of Clusters (K)")
    return f.fig


def plot_cluster_fig(data, columns, covar_type_tied_labels_k_bics,
                     show_ticks=False):
    """
    Creates cluster plot for the user data using label assignment provided, as a 2-row x 3-col scatter plot.

    Parameters
    ----------
    data: Pandas DataFrame
        User data file as a Pandas DataFrame
    columns: list(str)
        Column numbers from `data` to use as the x and y axes for the plot. Only the first two elements of the list
        are used.
    covar_type_tied_labels_k_bics: list((str, bool, list(int), int, float))
        [(covar_type, covar_tied, labels, k, bic), ... ]
    show_ticks: bool
        Show or hide tick marks on x and y axes.

    Returns
    -------
    Matplotlib Figure object.

    """
    sns.set(context='talk', style='white')
    columns = columns[:2]

    fig = plt.figure()
    placement = {'full': {True: 1, False: 4}, 'diag': {True: 2, False: 5}, 'spher': {True: 3, False: 6}}

    lim_left = data[columns[0]].min()
    lim_right = data[columns[0]].max()
    lim_bottom = data[columns[1]].min()
    lim_top = data[columns[1]].max()

    covar_type_tied_labels_k_bics = list(covar_type_tied_labels_k_bics)

    bics = [x[4] for x in covar_type_tied_labels_k_bics]
    max_bic = max(bics)

    for covar_type, covar_tied, labels, k, bic in covar_type_tied_labels_k_bics:
        plt.subplot(2, 3, placement[covar_type][covar_tied])
        plt.scatter(data[columns[0]], data[columns[1]], c=labels, cmap=plt.cm.rainbow, s=10)
        plt.xlabel(columns[0])
        plt.ylabel(columns[1])
        plt.xlim(left=lim_left, right=lim_right)
        plt.ylim(bottom=lim_bottom, top=lim_top)
        if show_ticks is False:
            plt.xticks([])
            plt.yticks([])
        title = '{}-{}, K={}\nBIC: {:,.1f}'.format(covar_type.capitalize(), ['Untied', 'Tied'][covar_tied], k, bic)
        if bic == max_bic:
            plt.title(title, fontweight='bold')
        else:
            plt.title(title)
    plt.tight_layout()
    return fig


def plot_single_cluster_fig(data, columns, labels, bic, k, show_ticks=False):
    """
    Creates cluster plot for the best label assignment based on BIC score.

    Parameters
    ----------
    data: Pandas DataFrame - User data file as a Pandas DataFrame
    columns: list(str) - Column numbers from to use as the plot's x and y axes.
    labels: list(int) - labels of the single task
    bic: int - task's BIC score
    show_ticks: bool - Show or hide tick marks on x and y axes.

    Returns
    -------
    Matplotlib Figure object.

    """
    sns.set(context='talk', style='white')
    columns = columns[:2]

    fig = plt.figure()
    lim_left = data[columns[0]].min()
    lim_right = data[columns[0]].max()
    lim_bottom = data[columns[1]].min()
    lim_top = data[columns[1]].max()

    plt.scatter(data[columns[0]], data[columns[1]], c=labels, cmap=plt.cm.rainbow, s=10)
    plt.xlabel(columns[0])
    plt.ylabel(columns[1])
    plt.xlim(left=lim_left, right=lim_right)
    plt.ylim(bottom=lim_bottom, top=lim_top)
    if show_ticks is False:
        plt.xticks([])
        plt.yticks([])
    title = "K={}\nBIC: {:,.1f}".format(k, bic)
    plt.title(title)
    plt.tight_layout()
    return fig

def plot_correlation_fig(data):
    """
    Creates a correlation heat map for all columns in user data.

    Parameters
    ----------
    data: Pandas DataFrame
        User data file as a Pandas DataFrame

    Returns
    -------
    Matplotlib Figure object.
    """
    sns.set(context='talk', style='white')
    fig = plt.figure()
    sns.heatmap(data.corr(), vmin=-1, vmax=1)
    plt.tight_layout()
    return fig


def plot_count_fig(tasks):
    """
    Create count plot, as a 2-row x 3-col bar plot of data points for each k in each covar.

    Parameters
    ----------
    tasks: list(dict)

    Returns
    -------
    Matplotlib Figure object.
    """
    sns.set(context='talk', style='whitegrid')
    df = pd.DataFrame(filter_dict_list_by_keys(tasks, ['k', 'covar_type', 'covar_tied']))
    df = df.loc[:, ['k', 'covar_type', 'covar_tied', 'bic', 'aic']]
    df['covar_type'] = [x.capitalize() for x in df['covar_type']]
    df['covar_tied'] = [['Untied', 'Tied'][x] for x in df['covar_tied']]
    f = sns.factorplot(x='k', kind='count', col='covar_type', row='covar_tied', data=df,
                      row_order=['Tied', 'Untied'], col_order=['Full', 'Diag', 'Spher'], legend=True, legend_out=True,
                      palette='Blues_d')
    f.set_titles("{col_name}-{row_name}")
    f.set_xlabels("Num. of Clusters (K)")
    return f.fig


def plot_spatial_cluster_fig(data, covar_type_tied_labels_k):
    """ Creates a 3x2 plot spatial plot using labels as the color """
    sns.set(context='talk', style='white')
    data.columns = [c.lower() for c in data.columns]
    fig = plt.figure()
    placement = {'full': {True: 1, False: 4}, 'diag': {True: 2, False: 5}, 'spher': {True: 3, False: 6}}

    lim_left = data['longitude'].min()
    lim_right = data['longitude'].max()
    lim_bottom = data['latitude'].min()
    lim_top = data['latitude'].max()
    for covar_type, covar_tied, labels, k in covar_type_tied_labels_k:
        plt.subplot(2, 3, placement[covar_type][covar_tied])
        plt.scatter(data['longitude'], data['latitude'], c=labels, cmap=plt.cm.rainbow, s=10)
        plt.xlim(left=lim_left, right=lim_right)
        plt.ylim(bottom=lim_bottom, top=lim_top)
        plt.xticks([])
        plt.yticks([])
        plt.xlabel('Longitude')
        plt.ylabel('Latitude')
        plt.title('{}-{}, K={}'.format(covar_type.capitalize(), ['Untied', 'Tied'][covar_tied], k))
    plt.tight_layout()
    return fig


def spatial_columns_exist(data):
    """ Returns True if one of each SPATIAL_COLUMNS exist in data (Pandas DataFrame). """
    columns = [c.lower() for c in data.columns]
    exist = [c in columns for c in SPATIAL_COLUMNS]
    return sum(exist) == 2


def fig_to_png_response(fig):
    """ Converts a matplotlib figure to an http respose png. """
    output = fig_to_png(fig)
    response = make_response(output.getvalue())
    response.mimetype = 'image/png'
    return response


def fig_to_png(fig):
    """ Converts a matplotlib figure to a png (byte stream). """
    canvas = FigureCanvas(fig)
    output = io.BytesIO()
    canvas.print_png(output)
    return output


def png_for_template(png):
    """
    Encodes a png (byte stream) so it can be passed to Jinja HTML template

    Usage in HTML:  <img src="data:image/png;base64,{{output}}"/>
    """
    output = base64.b64encode(png.getvalue())
    output = urllib.parse.quote(output)
    return output


""" File management functions """


def allowed_file(filename):
    """
    Checks filename to figure out if the file extension is in ALLOWED_EXTENSIONS global variable.

    Parameters
    ----------
    filename: str
        Name of the file with the extension, example: file.csv

    Returns
    -------
    bool
        True if file extension is allowed. False otherwise.
    """
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def generate_s3_file_key(job_id, filename):
    """
    Generates a unique key for use in Eucalyptus S3.

    Parameters
    ----------
    job_id: str
    filename: str
        Name of the file with the extension, example: file.csv

    Returns
    -------
    str
        file key for Eucalyptus S3

    """
    return '{}/{}/{}'.format(UPLOAD_FOLDER, job_id, filename)


def upload_to_s3(filepath, filename, job_id):
    """
    Uploads a file to Eucalyptus S3.

    Parameters
    ----------
    filepath: str
        Local path to the file
    filename: str
        Name of the file with the extension, example: file.csv
    job_id: str

    Returns
    -------
    str
        Eucalyptus S3 key generated for this file
    """
    s3_file_key = generate_s3_file_key(job_id, filename)

    """ Amazon S3 code """
    # s3 = boto3.resource('s3')
    # s3.meta.client.upload_file(filepath, S3_BUCKET, s3_file_key)

    """ Eucalyptus S3 code """
    s3conn = boto.connect_walrus(aws_access_key_id=EUCA_KEY_ID, aws_secret_access_key=EUCA_SECRET_KEY, is_secure=False,
                                 port=8773, path=EUCA_S3_PATH, host=EUCA_S3_HOST)
    euca_bucket = s3conn.get_bucket(S3_BUCKET)
    k = boto.s3.key.Key(bucket=euca_bucket, name=s3_file_key)
    k.set_contents_from_filename(filepath)
    return s3_file_key


def s3_to_df(s3_file_key):
    """
    Downloads file from S3 and converts it to a Pandas DataFrame. Deletes the file from local disk when done.

    Parameters
    ----------
    s3_file_key: str
        Eucalyptus S3 file key

    Returns
    -------
    Pandas DataFrame
    """
    # Add random number to file name to avoid collisions with other processes on the same machine
    filename = '/tmp/{}_{}'.format(s3_file_key.replace('/', '_'), random.randint(1, 1e6))

    """ Amazon S3 code """
    # s3 = boto3.client('s3')
    # s3.download_file(S3_BUCKET, s3_file_key, filename)

    """ Eucalyptus S3 code """
    s3conn = boto.connect_walrus(aws_access_key_id=EUCA_KEY_ID, aws_secret_access_key=EUCA_SECRET_KEY, is_secure=False,
                                 port=8773, path=EUCA_S3_PATH, host=EUCA_S3_HOST)
    euca_bucket = s3conn.get_bucket(S3_BUCKET)
    k = boto.s3.key.Key(bucket=euca_bucket, name=s3_file_key)
    k.get_contents_to_filename(filename)

    df = pd.read_csv(filename)
    os.remove(filename)
    return df


def job_to_data(job_id):
    job = mongo_get_job(job_id)
    s3_file_key = job['s3_file_key']
    return s3_to_df(s3_file_key)


def get_viz_columns(job, x_axis, y_axis):
    # Return user selected visualization columns
    if x_axis is not None and y_axis is not None:
        return [x_axis, y_axis]
    # Return the first two clustering columns
    job_columns = job['columns']
    preferred_columns = [c for c in job_columns if c.lower().strip() not
                         in EXCLUDE_COLUMNS][:2]
    if len(preferred_columns) == 2:
        return preferred_columns
    if len(job_columns) >= 2:
        return job_columns[:2]
    # Return the first two data columns
    data = job_to_data(job['_id'])
    all_columns = data.columns
    preferred_columns = [c for c in all_columns if c.lower().strip() not
                         in EXCLUDE_COLUMNS][:2]
    if len(preferred_columns) == 2:
        return preferred_columns
    if len(all_columns) >= 2:
        return all_columns[:2]
    # Case with only 1 column
    raise ValueError('Too few columns')
