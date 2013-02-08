# Authors: Alexandre Gramfort <gramfort@nmr.mgh.harvard.edu>
#
# License: BSD (3-clause)

import numpy as np
from scipy import linalg

import logging
logger = logging.getLogger('mne')

from . import fiff, Epochs, verbose
from .fiff.pick import pick_types, pick_types_forward
from .fiff.proj import Projection
from .event import make_fixed_length_events
from .parallel import parallel_func
from .cov import _check_n_samples
from .forward import is_fixed_orient, _subject_from_forward
from .source_estimate import SourceEstimate
from .fiff.proj import make_projector, make_eeg_average_ref_proj
from .fiff import FIFF


def read_proj(fname):
    """Read projections from a FIF file.

    Parameters
    ----------
    fname : string
        The name of file containing the projections vectors.

    Returns
    -------
    projs : list
        The list of projection vectors.
    """
    fid, tree, _ = fiff.fiff_open(fname)
    projs = fiff.proj.read_proj(fid, tree)
    return projs


def write_proj(fname, projs):
    """Write projections to a FIF file.

    Parameters
    ----------
    fname : string
        The name of file containing the projections vectors.

    projs : list
        The list of projection vectors.
    """
    fid = fiff.write.start_file(fname)
    fiff.proj.write_proj(fid, projs)
    fiff.write.end_file(fid)


@verbose
def _compute_proj(data, info, n_grad, n_mag, n_eeg, desc_prefix, verbose=None):
    mag_ind = pick_types(info, meg='mag', exclude='bads')
    grad_ind = pick_types(info, meg='grad', exclude='bads')
    eeg_ind = pick_types(info, meg=False, eeg=True, exclude='bads')

    if (n_grad > 0) and len(grad_ind) == 0:
        logger.info("No gradiometers found. Forcing n_grad to 0")
        n_grad = 0
    if (n_mag > 0) and len(mag_ind) == 0:
        logger.info("No magnetometers found. Forcing n_mag to 0")
        n_mag = 0
    if (n_eeg > 0) and len(eeg_ind) == 0:
        logger.info("No EEG channels found. Forcing n_eeg to 0")
        n_eeg = 0

    ch_names = info['ch_names']
    grad_names, mag_names, eeg_names = ([ch_names[k] for k in ind]
                                     for ind in [grad_ind, mag_ind, eeg_ind])

    projs = []
    for n, ind, names, desc in zip([n_grad, n_mag, n_eeg],
                                   [grad_ind, mag_ind, eeg_ind],
                                   [grad_names, mag_names, eeg_names],
                                   ['planar', 'axial', 'eeg']):
        if n == 0:
            continue
        data_ind = data[ind][:, ind]
        U = linalg.svd(data_ind, full_matrices=False,
                       overwrite_a=True)[0][:, :n]
        for k, u in enumerate(U.T):
            proj_data = dict(col_names=names, row_names=None,
                             data=u[np.newaxis, :], nrow=1, ncol=u.size)
            this_desc = "%s-%s-PCA-%02d" % (desc, desc_prefix, k + 1)
            logger.info("Adding projection: %s" % this_desc)
            proj = Projection(active=False, data=proj_data, desc=this_desc, kind=1)
            projs.append(proj)

    return projs


@verbose
def compute_proj_epochs(epochs, n_grad=2, n_mag=2, n_eeg=2, n_jobs=1,
                        verbose=None):
    """Compute SSP (spatial space projection) vectors on Epochs

    Parameters
    ----------
    epochs : instance of Epochs
        The epochs containing the artifact
    n_grad : int
        Number of vectors for gradiometers
    n_mag : int
        Number of vectors for gradiometers
    n_eeg : int
        Number of vectors for gradiometers
    n_jobs : int
        Number of jobs to use to compute covariance
    verbose : bool, str, int, or None
        If not None, override default verbose level (see mne.verbose).

    Returns
    -------
    projs: list
        List of projection vectors
    """
    # compute data covariance
    data = _compute_cov_epochs(epochs, n_jobs)
    event_id = epochs.event_id
    if event_id is None or len(event_id.keys()) == 0:
        event_id = '0'
    elif len(event_id.keys()) == 1:
        event_id = str(event_id.values()[0])
    else:
        event_id = 'Multiple-events'
    desc_prefix = "%s-%-.3f-%-.3f" % (event_id, epochs.tmin, epochs.tmax)
    return _compute_proj(data, epochs.info, n_grad, n_mag, n_eeg, desc_prefix)


def _compute_cov_epochs(epochs, n_jobs):
    """Helper function for computing epochs covariance"""
    parallel, p_fun, _ = parallel_func(np.dot, n_jobs)
    data = parallel(p_fun(e, e.T) for e in epochs)
    n_epochs = len(data)
    if n_epochs == 0:
        raise RuntimeError('No good epochs found')

    n_chan, n_samples = epochs.__iter__().next().shape
    _check_n_samples(n_samples * n_epochs, n_chan)
    data = sum(data)
    return data


@verbose
def compute_proj_evoked(evoked, n_grad=2, n_mag=2, n_eeg=2, verbose=None):
    """Compute SSP (spatial space projection) vectors on Evoked

    Parameters
    ----------
    evoked : instance of Evoked
        The Evoked obtained by averaging the artifact
    n_grad : int
        Number of vectors for gradiometers
    n_mag : int
        Number of vectors for gradiometers
    n_eeg : int
        Number of vectors for gradiometers
    verbose : bool, str, int, or None
        If not None, override default verbose level (see mne.verbose).

    Returns
    -------
    projs : list
        List of projection vectors
    """
    data = np.dot(evoked.data, evoked.data.T)  # compute data covariance
    desc_prefix = "%-.3f-%-.3f" % (evoked.times[0], evoked.times[-1])
    return _compute_proj(data, evoked.info, n_grad, n_mag, n_eeg, desc_prefix)


@verbose
def compute_proj_raw(raw, start=0, stop=None, duration=1, n_grad=2, n_mag=2,
                     n_eeg=0, reject=None, flat=None, n_jobs=1, verbose=None):
    """Compute SSP (spatial space projection) vectors on Raw

    Parameters
    ----------
    raw : instance of Raw
        A raw object to use the data from
    start : float
        Time (in sec) to start computing SSP
    stop : float
        Time (in sec) to stop computing SSP
        None will go to the end of the file
    duration : float
        Duration (in sec) to chunk data into for SSP
        If duration is None, data will not be chunked.
    n_grad : int
        Number of vectors for gradiometers
    n_mag : int
        Number of vectors for gradiometers
    n_eeg : int
        Number of vectors for gradiometers
    reject : dict
        Epoch rejection configuration (see Epochs)
    flat : dict
        Epoch flat configuration (see Epochs)
    n_jobs : int
        Number of jobs to use to compute covariance
    verbose : bool, str, int, or None
        If not None, override default verbose level (see mne.verbose).

    Returns
    -------
    projs: list
        List of projection vectors
    """
    if duration is not None:
        events = make_fixed_length_events(raw, 999, start, stop, duration)
        epochs = Epochs(raw, events, None, tmin=0., tmax=duration,
                        picks=pick_types(raw.info, meg=True, eeg=True,
                                         eog=True, ecg=True, emg=True,
                                         exclude='bads'),
                        reject=reject, flat=flat)
        data = _compute_cov_epochs(epochs, n_jobs)
        info = epochs.info
        if not stop:
            stop = raw.n_times / raw.info['sfreq']
    else:
        # convert to sample indices
        start = max(raw.time_as_index(start)[0], 0)
        stop = raw.time_as_index(stop)[0] if stop else raw.n_times
        stop = min(stop, raw.n_times)
        data, times = raw[:, start:stop]
        _check_n_samples(stop - start, data.shape[0])
        data = np.dot(data, data.T)  # compute data covariance
        info = raw.info
        # convert back to times
        start = start / raw.info['sfreq']
        stop = stop / raw.info['sfreq']

    desc_prefix = "Raw-%-.3f-%-.3f" % (start, stop)
    projs = _compute_proj(data, info, n_grad, n_mag, n_eeg, desc_prefix)
    return projs


def sensitivity_map(fwd, projs=None, ch_type='grad', mode='fixed', exclude=[],
                    verbose=None):
    """Compute sensitivity map

    Such maps are used to know how much sources are visible by a type
    of sensor, and how much projections shadow some sources.

    Parameters
    ----------
    fwd : dict
        The forward operator. Must be free- and surface-oriented.
    projs : list
        List of projection vectors.
    ch_type : 'grad' | 'mag' | 'eeg'
        The type of sensors to use.
    mode : str
        The type of sensitivity map computed. See manual. Should be 'free',
        'fixed', 'ratio', 'radiality', 'angle', 'remaining', or 'dampening'
        corresponding to the argument --map 1, 2, 3, 4, 5, 6 and 7 of the
        command mne_sensitivity_map.
    exclude : list of string | str
        List of channels to exclude. If empty do not exclude any (default).
        If 'bads', exclude channels in fwd['info']['bads'].
    verbose : bool, str, int, or None
        If not None, override default verbose level (see mne.verbose).

    Return
    ------
    stc : SourceEstimate
        The sensitivity map as a SourceEstimate instance for
        visualization.
    """
    # check strings
    if not ch_type in ['eeg', 'grad', 'mag']:
        raise ValueError("ch_type should be 'eeg', 'mag' or 'grad (got %s)"
                          % ch_type)
    if not mode in ['free', 'fixed', 'ratio', 'radiality', 'angle',
                    'remaining', 'dampening']:
        raise ValueError('Unknown mode type (got %s)' % mode)

    # check forward
    if not fwd['surf_ori']:
        raise ValueError('fwd should be surface oriented')
    if is_fixed_orient(fwd):
        raise ValueError('fwd should not have fixed orientation')

    # limit forward
    if ch_type == 'eeg':
        fwd = pick_types_forward(fwd, meg=False, eeg=True, exclude=exclude)
    else:
        fwd = pick_types_forward(fwd, meg=ch_type, eeg=False, exclude=exclude)

    gain = fwd['sol']['data']

    # Make sure EEG has average
    if ch_type == 'eeg':
        if projs is None or \
                not any([p['kind'] == FIFF.FIFFV_MNE_PROJ_ITEM_EEG_AVREF
                         for p in projs]):
            eeg_ave = [make_eeg_average_ref_proj(fwd['info'])]
        projs = eeg_ave if projs is None else projs + eeg_ave

    # Construct the projector
    if projs is not None:
        proj, ncomp, U = make_projector(projs, fwd['sol']['row_names'],
                                              include_active=True)
        # do projection for most types
        if mode not in ['angle', 'remaining', 'dampening']:
            gain = np.dot(proj, gain)

    # can only run the last couple methods if there are projectors
    elif mode in ['angle', 'remaining', 'dampening']:
        raise ValueError('No projectors used, cannot compute %s' % mode)

    n_sensors, n_dipoles = gain.shape
    n_locations = n_dipoles // 3
    sensitivity_map = np.empty(n_locations)

    for k in xrange(n_locations):
        gg = gain[:, 3 * k:3 * (k + 1)]
        if mode != 'fixed':
            s = linalg.svd(gg, full_matrices=False, compute_uv=False)
        if mode == 'free':
            sensitivity_map[k] = s[0]
        else:
            gz = linalg.norm(gg[:, 2])  # the normal component
            if mode == 'fixed':
                sensitivity_map[k] = gz
            elif mode == 'ratio':
                sensitivity_map[k] = gz / s[0]
            elif mode == 'radiality':
                sensitivity_map[k] = 1. - (gz / s[0])
            else:
                if mode == 'angle':
                    co = linalg.norm(np.dot(gg[:, 2], U))
                    sensitivity_map[k] = co / gz
                else:
                    p = linalg.norm(np.dot(proj, gg[:, 2]))
                    if mode == 'remaining':
                        sensitivity_map[k] = p / gz
                    elif mode == 'dampening':
                        sensitivity_map[k] = 1. - p / gz
                    else:
                        raise ValueError('Unknown mode type (got %s)' % mode)

    # only normalize fixed and free methods
    if mode in ['fixed', 'free']:
        sensitivity_map /= np.max(sensitivity_map)

    vertices = [fwd['src'][0]['vertno'], fwd['src'][1]['vertno']]
    subject = _subject_from_forward(fwd)
    stc = SourceEstimate(sensitivity_map[:, np.newaxis],
                         vertices=vertices, tmin=0, tstep=1,
                         subject=subject)
    return stc
