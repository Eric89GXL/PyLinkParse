# -*- coding: utf-8 -*-
"""EDF Raw class"""

import numpy as np
from os import path as op
import ctypes as ct
from datetime import datetime
from functools import partial


from ._edf2py import (edf_open_file, edf_close_file, edf_get_next_data,
                      edf_get_preamble_text_length,
                      edf_get_preamble_text, edf_get_recording_data,
                      edf_get_sample_data, edf_get_event_data)
from .._baseraw import _BaseRaw
from ._defines import event_constants
from . import _defines as defines


class RawEDF(_BaseRaw):
    """Represent EyeLink EDF files in Python

    Parameters
    ----------
    fname : str
        The name of the EDF file.
    """
    def __init__(self, fname):
        info, discrete, times, samples = _read_raw_edf(fname)
        self.info = info
        self.discrete = discrete
        self._times = times
        self._samples = samples
        _BaseRaw.__init__(self)  # perform sanity checks


class _edf_open(object):
    """Context manager for opening EDF files"""
    def __init__(self, fname):
        self.fname = op.normpath(op.abspath(fname).encode("ASCII"))
        self.fid = None

    def __enter__(self):
        error_code = ct.c_int(1)
        self.fid = edf_open_file(self.fname, 2, 1, 1, ct.byref(error_code))
        if self.fid is None or error_code.value != 0:
            raise IOError('Could not open file "%s": (%s, %s)'
                          % (self.fname, self.fid, error_code.value))
        return self.fid

    def __exit__(self, type, value, traceback):
        if self.fid is not None:
            result = edf_close_file(self.fid)
            if result != 0:
                raise IOError('File "%s" could not be closed' % self.fname)


_ets2pp = dict(SAMPLE_TYPE='sample', ENDFIX='fixations', ENDSACC='saccades',
               ENDBLINK='blinks', BUTTONEVENT='buttons', INPUTEVENT='inputs')


def _read_raw_edf(fname):
    """Read data from raw EDF file into pyeparse format"""
    if not op.isfile(fname):
        raise IOError('File "%s" does not exist' % fname)

    #
    # First pass: get the number of each type of sample
    #
    n_samps = dict()
    offsets = dict()
    for key in _ets2pp.values():
        n_samps[key] = 0
        offsets[key] = 0
    with _edf_open(fname) as edf:
        etype = None
        while etype != event_constants.get('NO_PENDING_ITEMS'):
            etype = edf_get_next_data(edf)
            if etype not in event_constants:
                raise RuntimeError('unknown type %s' % event_constants[etype])
            ets = event_constants[etype]
            if ets in _ets2pp:
                n_samps[_ets2pp[ets]] += 1

    #
    # Now let's actually read in the data
    #
    with _edf_open(fname) as edf:
        info = _parse_preamble(edf)
        info['discrete_fields'] = dict()
        etype = None
        res = dict(info=info, samples=None, n_samps=n_samps, offsets=offsets,
                   messages=dict(time=[], msg=[]), edf_fields=dict())
        res['samples'] = np.empty((4, 1000), np.float64)
        while etype != event_constants.get('NO_PENDING_ITEMS'):
            etype = edf_get_next_data(edf)
            if etype not in event_constants:
                raise RuntimeError('unknown type %s' % event_constants[etype])
            ets = event_constants[etype]
            _element_handlers[ets](edf, res)

    #
    # Put info and discrete into correct output format
    #
    discrete = dict()
    info = res['info']
    info['event_types'] = ('saccades', 'fixations', 'blinks',
                           'buttons', 'inputs')
    info['sample_fields'] = info['sample_fields'][1:]  # omit time
    for key in info['event_types']:
        src = res[key]
        discrete[key] = dict()
        for ii, sub_key in enumerate(info['discrete_fields'][key]):
            discrete[key][sub_key] = src[ii]
    discrete['messages'] = res['messages']
    discrete['messages']['time'] = np.array(discrete['messages']['time'],
                                            np.float64)

    #
    # fix sample times
    #
    # XXX This will need to be fixed for files with multiple starts/stops...
    data = res['samples'][1:]
    times = res['samples'][0]
    t_zero = times[0]
    _adjust_time(times, t_zero)
    for key in list(info['event_types']) + ['messages']:
        for sub_key in ('stime', 'etime', 'time'):
            if sub_key in discrete[key]:
                _adjust_time(discrete[key][sub_key], t_zero)

    # now we corect our time offsets
    return info, discrete, times, data


def _adjust_time(x, t_zero):
    """Helper to adjust time, inplace"""
    x -= t_zero
    x /= 1000.0


def _extract_sys_info(line):
    """Aux function for preprocessing sys info lines"""
    return line[line.find(':'):].strip(': \r\n')


def _parse_preamble(edf):
    tlen = edf_get_preamble_text_length(edf)
    txt = ct.create_string_buffer(tlen)
    edf_get_preamble_text(edf, txt, tlen + 1)
    preamble_lines = txt.value.decode('ASCII').split('\n')
    info = dict()
    for line in preamble_lines:
        if '!MODE'in line:
            line = line.split()
            info['eye'], info['sfreq'] = line[-1], float(line[-4])
        elif 'DATE:' in line:
            line = _extract_sys_info(line).strip()
            fmt = '%a %b  %d %H:%M:%S %Y'
            info['meas_date'] = datetime.strptime(line, fmt)
        elif 'VERSION:' in line:
            info['version'] = _extract_sys_info(line)
        elif 'CAMERA:' in line:
            info['camera'] = _extract_sys_info(line)
        elif 'SERIAL NUMBER:' in line:
            info['serial'] = _extract_sys_info(line)
        elif 'CAMERA_CONFIG:' in line:
            info['camera_config'] = _extract_sys_info(line)
    return info


def _to_list(element, keys, idx):
    """Return a list of particular fields of an EyeLink data element"""
    out = list()
    for k in keys:
        v = getattr(element, k)
        if hasattr(v, '_length_'):
            out.append(v[idx])
        else:
            out.append(v)
    return out


def _sample_fields_available(sflags):
    """
    Returns a dict where the keys indicate fields (or field groups) of a
    sample; the value for each indicates if the field has been populated
    with data and can be considered as useful information.
    """
    return dict(
        time=bool(sflags & defines.SAMPLE_TIMESTAMP),  # sample time
        gx=bool(sflags & defines.SAMPLE_GAZEXY),  # gaze X position
        gy=bool(sflags & defines.SAMPLE_GAZEXY),  # gaze Y position
        pa=bool(sflags & defines.SAMPLE_PUPILSIZE),  # pupil size
        left=bool(sflags & defines.SAMPLE_LEFT),  # left eye data
        right=bool(sflags & defines.SAMPLE_RIGHT),  # right eye data
        pupilxy=bool(sflags & defines.SAMPLE_PUPILXY),  # raw eye position
        hrefxy=bool(sflags & defines.SAMPLE_HREFXY),  # href eye position
        gazeres=bool(sflags & defines.SAMPLE_GAZERES),  # x,y pixels per deg
        status=bool(sflags & defines.SAMPLE_STATUS),  # sample status
        inputs=bool(sflags & defines.SAMPLE_INPUTS),  # sample inputs
        button=bool(sflags & defines.SAMPLE_BUTTONS),  # sample buttons
        headpos=bool(sflags & defines.SAMPLE_HEADPOS),  # sample head pos
        # if this flag is set for the sample add .5ms to the sample time
        addoffset=bool(sflags & defines.SAMPLE_ADD_OFFSET),
        # reserved variable-length tagged
        tagged=bool(sflags & defines.SAMPLE_TAGGED),
        # user-defineabe variable-length tagged
        utagged=bool(sflags & defines.SAMPLE_UTAGGED),
    )


'''
def _event_fields_available(eflags):
    """
    Returns a dict where the keys indicate fields (or field groups) of an
    EDF event; the value for each indicates if the field has been populated
    with data and can be considered as useful information.
    """
    return dict(
        endtime=bool(eflags & defines.READ_ENDTIME),  # end time
        gres=bool(eflags & defines.READ_GRES),  # gaze resolution xy
        size=bool(eflags & defines.READ_SIZE),  # pupil size
        vel=bool(eflags & defines.READ_VEL),  # velocity (avg, peak)
        status=bool(eflags & defines.READ_STATUS),  # status (error word)
        beg=bool(eflags & defines.READ_BEG),  # start data for vel,size,gres
        end=bool(eflags & defines.READ_END),  # end data for vel,size,gres
        avg=bool(eflags & defines.READ_AVG),  # avg pupil size, velocity
        pupilxy=bool(eflags & defines.READ_PUPILXY),  # position eye data
        hrefxy=bool(eflags & defines.READ_HREFXY),
        gazexy=bool(eflags & defines.READ_GAZEXY),
        begpos=bool(eflags & defines.READ_BEGPOS),
        endpos=bool(eflags & defines.READ_ENDPOS),
        avgpos=bool(eflags & defines.READ_AVGPOS),
    )
'''


_pp2el = dict(eye='eye', time='time', stime='sttime', etime='entime',
              xpos='gx', ypos='gy', sxp='gstx', syp='gsty',
              exp='genx', eyp='geny', axp='gavx', ayp='gavy',
              pv='pvel', ps='pa', aps='avg', buttons='buttons', input='input')
_el2pp = dict()
for key, val in _pp2el.items():
    _el2pp[val] = key


#
## EDF File Handlers
#

def _handle_recording_info(edf, res):
    """RECORDING_INFO"""
    info = res['info']
    e = edf_get_recording_data(edf).contents
    if e.state == 0:  # recording stopped
        return
    if 'sfreq' in info:
        assert e.sample_rate == info['sfreq']
        assert defines.eye_constants[e.eye] == info['eye']
        x = str(defines.pupil_constants[e.pupil_type])
        assert x == info['ps_units']
        return
    info['sfreq'] = e.sample_rate
    info['ps_units'] = defines.pupil_constants[e.pupil_type]
    info['eye'] = defines.eye_constants[e.eye]
    res['eye_idx'] = e.eye - 1

    # Figure out sample flags
    sflags = _sample_fields_available(e.sflags)
    edf_fields = ['time', 'gx', 'gy', 'pa']  # XXX Expand?
    edf_fields = [field for field in edf_fields if sflags[field]]
    sample_fld = [_el2pp[field] for field in edf_fields]
    res['edf_sample_fields'] = edf_fields
    res['info']['sample_fields'] = sample_fld
    res['samples'] = np.empty((len(edf_fields), res['n_samps']['sample']),
                              np.float64)


def _handle_sample(edf, res):
    """SAMPLE_TYPE"""
    e = edf_get_sample_data(edf).contents
    off = res['offsets']['sample']
    res['samples'][:, off] = _to_list(e, res['edf_sample_fields'],
                                      res['eye_idx'])
    res['offsets']['sample'] += 1


def _handle_message(edf, res):
    """MESSAGEEVENT"""
    e = edf_get_event_data(edf).contents
    msg = ct.string_at(ct.byref(e.message[0]), e.message.contents.len + 1)[2:]
    res['messages']['time'].append(e.sttime)
    res['messages']['msg'].append(msg)


def _handle_end(edf, res, name):
    """ENDSACC, ENDFIX, ENDBLINK, BUTTONS, INPUT"""
    if name not in res['info']['discrete_fields']:
        # XXX This should be changed to support given fields
        if name == 'saccades':
            f = ['eye', 'sttime', 'entime',
                 'gstx', 'gsty', 'genx', 'geny', 'pvel']
        elif name == 'fixations':
            f = ['eye', 'sttime', 'entime', 'gavx', 'gavy']
        elif name == 'blinks':
            f = ['eye', 'sttime', 'entime']
        elif name == 'buttons':
            f = ['sttime', 'buttons']
        elif name == 'inputs':
            f = ['sttime', 'input']
        else:
            raise KeyError('Unknown name %s' % name)
        res['edf_fields'][name] = f
        res['info']['discrete_fields'][name] = [_el2pp[field] for field in f]
        res[name] = np.empty((len(f), res['n_samps'][name]), np.float64)
    e = edf_get_event_data(edf).contents
    vals = _to_list(e, res['edf_fields'][name], res['eye_idx'])
    off = res['offsets'][name]
    res[name][:, off] = vals
    res['offsets'][name] += 1


def _handle_pass(edf, res):
    """Events we don't care about or haven't had to care about yet"""
    pass


def _handle_fixation_update(edf, res):
    """FIXUPDATE"""
    raise NotImplementedError


# element_handlers maps the various EDF file element types to the
# element handler function that should be called.
#
_element_handlers = dict(RECORDING_INFO=_handle_recording_info,
                         SAMPLE_TYPE=_handle_sample,
                         MESSAGEEVENT=_handle_message,
                         ENDFIX=partial(_handle_end, name='fixations'),
                         ENDSACC=partial(_handle_end, name='saccades'),
                         ENDBLINK=partial(_handle_end, name='blinks'),
                         BUTTONEVENT=partial(_handle_end, name='buttons'),
                         INPUTEVENT=partial(_handle_end, name='inputs'),
                         STARTFIX=_handle_pass,
                         STARTSACC=_handle_pass,
                         STARTBLINK=_handle_pass,
                         STARTPARSE=_handle_pass,
                         FIXUPDATE=_handle_pass,
                         ENDPARSE=_handle_pass,
                         NO_PENDING_ITEMS=_handle_pass,  # context manager
                         BREAKPARSE=_handle_pass,
                         STARTSAMPLES=_handle_pass,
                         ENDSAMPLES=_handle_pass,
                         STARTEVENTS=_handle_pass,
                         ENDEVENTS=_handle_pass,
                         )
