# Copyright 2021 The Flax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Serialization utilities for Jax.

All Flax classes that carry state (e.g. ,Optimizer) can be turned into a
state dict of numpy arrays for easy serialization.
"""
import collections
import enum
import jax
import msgpack
import numpy as np
from numpy.lib.arraysetops import isin


_STATE_DICT_REGISTRY = {}


class _NamedTuple:
  """Fake type marker for namedtuple for registry."""
  pass


def _is_namedtuple(x):
  """Duck typing test for namedtuple factory-generated objects."""
  return isinstance(x, tuple) and hasattr(x, '_fields')


def from_state_dict(target, state):
  """Restores the state of the given target using a state dict.

  This function takes the current target as an argument. This
  lets us know the exact structure of the target,
  as well as lets us add assertions that shapes and dtypes don't change.

  In practice, none of the leaf values in `target` are actually
  used. Only the tree structure, shapes and types.

  Args:
    target: the object of which the state should be restored.
    state: a dictionary generated by `to_state_dict` with the desired new
           state for `target`.
  Returns:
    A copy of the object with the restored state.
  """
  if _is_namedtuple(target):
    ty = _NamedTuple
  else:
    ty = type(target)
  if ty not in _STATE_DICT_REGISTRY:
    return state
  ty_from_state_dict = _STATE_DICT_REGISTRY[ty][1]
  return ty_from_state_dict(target, state)


def to_state_dict(target):
  """Returns a dictionary with the state of the given target."""
  if _is_namedtuple(target):
    ty = _NamedTuple
  else:
    ty = type(target)
  if ty not in _STATE_DICT_REGISTRY:
    return target

  ty_to_state_dict = _STATE_DICT_REGISTRY[ty][0]
  state_dict = ty_to_state_dict(target)
  assert isinstance(state_dict, dict), 'A state dict must be a Python dict.'
  for key in state_dict.keys():
    assert isinstance(key, str), 'A state dict must only have string keys.'
  return state_dict


def register_serialization_state(ty, ty_to_state_dict, ty_from_state_dict,
                                 override=False):
  """Register a type for serialization.

  Args:
    ty: the type to be registered
    ty_to_state_dict: a function that takes an instance of ty and
      returns its state as a dictionary.
    ty_from_state_dict: a function that takes an instance of ty and
      a state dict, and returns a copy of the instance with the restored state.
    override: override a previously registered serialization handler
      (default: False).
  """
  if ty in _STATE_DICT_REGISTRY and not override:
    raise ValueError(f'a serialization handler for "{ty.__name__}"'
                     ' is already registered')
  _STATE_DICT_REGISTRY[ty] = (ty_to_state_dict, ty_from_state_dict)


def _list_state_dict(xs):
  return {str(i): to_state_dict(x) for i, x in enumerate(xs)}


def _restore_list(xs, state_dict):
  if len(state_dict) != len(xs):
    raise ValueError('The size of the list and the state dict do not match,'
                     f' got {len(xs)} and {len(state_dict)}.')
  ys = []
  for i in range(len(state_dict)):
    y = from_state_dict(xs[i], state_dict[str(i)])
    ys.append(y)
  return ys


def _dict_state_dict(xs):
  return {key: to_state_dict(value) for key, value in xs.items()}


def _restore_dict(xs, states):
  return {key: from_state_dict(value, states[key])
          for key, value in xs.items()}


def _namedtuple_state_dict(nt):
  return {key: to_state_dict(getattr(nt, key)) for key in nt._fields}


def _restore_namedtuple(xs, state_dict):
  """Rebuild namedtuple from serialized dict."""
  if set(state_dict.keys()) == {'name', 'fields', 'values'}:
    # TODO(jheek): remove backward compatible named tuple restoration early 2022
    state_dict = {state_dict['fields'][str(i)]: state_dict['values'][str(i)]
                  for i in range(len(state_dict['fields']))}

  sd_keys = set(state_dict.keys())
  nt_keys = set(xs._fields)

  if sd_keys != nt_keys:
    raise ValueError('The field names of the state dict and the named tuple do not match,'
                     f' got {sd_keys} and {nt_keys}.')
  fields = {k: from_state_dict(getattr(xs, k), v) for k, v in state_dict.items()}
  return type(xs)(**fields)


register_serialization_state(dict, _dict_state_dict, _restore_dict)
register_serialization_state(list, _list_state_dict, _restore_list)
register_serialization_state(
    tuple, _list_state_dict,
    lambda xs, state_dict: tuple(_restore_list(list(xs), state_dict)))
register_serialization_state(_NamedTuple,
                             _namedtuple_state_dict,
                             _restore_namedtuple)


# On-the-wire / disk serialization format

# We encode state-dicts via msgpack, using its custom type extension.
# https://github.com/msgpack/msgpack/blob/master/spec.md
#
# - ndarrays and DeviceArrays are serialized to nested msgpack-encoded string
#   of (shape-tuple, dtype-name (e.g. 'float32'), row-major array-bytes).
#   Note: only simple ndarray types are supported, no objects or fields.
#
# - native complex scalars are converted to nested msgpack-encoded tuples
#   (real, imag).


def _ndarray_to_bytes(arr):
  """Save ndarray to simple msgpack encoding."""
  if isinstance(arr, jax.xla.DeviceArray):
    arr = np.array(arr)
  if arr.dtype.hasobject or arr.dtype.isalignedstruct:
    raise ValueError('Object and structured dtypes not supported '
                     'for serialization of ndarrays.')
  tpl = (arr.shape, arr.dtype.name, arr.tobytes('C'))
  return msgpack.packb(tpl, use_bin_type=True)


def _dtype_from_name(name):
  """Handle JAX bfloat16 dtype correctly."""
  if name == b'bfloat16':
    return jax.numpy.bfloat16
  else:
    return np.dtype(name)


def _ndarray_from_bytes(data):
  """Load ndarray from simple msgpack encoding."""
  shape, dtype_name, buffer = msgpack.unpackb(data, raw=True)
  return np.frombuffer(buffer,
                       dtype=_dtype_from_name(dtype_name),
                       count=-1,
                       offset=0).reshape(shape, order='C')


class _MsgpackExtType(enum.IntEnum):
  """Messagepack custom type ids."""
  ndarray = 1
  native_complex = 2
  npscalar = 3


def _msgpack_ext_pack(x):
  """Messagepack encoders for custom types."""
  if isinstance(x, (np.ndarray, jax.xla.DeviceArray)):
    return msgpack.ExtType(_MsgpackExtType.ndarray, _ndarray_to_bytes(x))
  if np.issctype(type(x)):
    # pack scalar as ndarray
    return msgpack.ExtType(_MsgpackExtType.npscalar,
                           _ndarray_to_bytes(np.asarray(x)))
  elif isinstance(x, complex):
    return msgpack.ExtType(_MsgpackExtType.native_complex,
                           msgpack.packb((x.real, x.imag)))
  return x


def _msgpack_ext_unpack(code, data):
  """Messagepack decoders for custom types."""
  if code == _MsgpackExtType.ndarray:
    return _ndarray_from_bytes(data)
  elif code == _MsgpackExtType.native_complex:
    complex_tuple = msgpack.unpackb(data)
    return complex(complex_tuple[0], complex_tuple[1])
  elif code == _MsgpackExtType.npscalar:
    ar = _ndarray_from_bytes(data)
    return ar[()]  # unpack ndarray to scalar
  return msgpack.ExtType(code, data)


# Chunking array leaves

# msgpack has a hard limit of 2**31 - 1 bytes per object leaf.  To circumvent
# this limit for giant arrays (e.g. embedding tables), we traverse the tree
# and break up arrays near the limit into flattened array chunks.

# True limit is 2**31 - 1, but leave a margin for encoding padding.
MAX_CHUNK_SIZE = 2**30


def _np_convert_in_place(d):
  """Convert any jax devicearray leaves to numpy arrays in place."""
  if isinstance(d, dict):
    for k, v in d.items():
      if isinstance(v, jax.xla.DeviceArray):
        d[k] = np.array(v)
      elif isinstance(v, dict):
        _np_convert_in_place(v)
  elif isinstance(d, jax.xla.DeviceArray):
    return np.array(d)
  return d


_tuple_to_dict = lambda tpl: dict([(str(x), y) for x, y in enumerate(tpl)])
_dict_to_tuple = lambda dct: tuple(dct[str(i)] for i in range(len(dct)))


def _chunk(arr):
  """Convert array to a canonical dictionary of chunked arrays."""
  chunksize = max(1, int(MAX_CHUNK_SIZE / arr.dtype.itemsize))
  data = {'__msgpack_chunked_array__': True,
          'shape': _tuple_to_dict(arr.shape)}
  flatarr = arr.reshape(-1)
  chunks = [flatarr[i:i + chunksize] for i in range(0, flatarr.size, chunksize)]
  data['chunks'] = _tuple_to_dict(chunks)
  return data


def _unchunk(data):
  """Convert canonical dictionary of chunked arrays back into array."""
  assert '__msgpack_chunked_array__' in data
  shape = _dict_to_tuple(data['shape'])
  flatarr = np.concatenate(_dict_to_tuple(data['chunks']))
  return flatarr.reshape(shape)


def _chunk_array_leaves_in_place(d):
  """Convert oversized array leaves to safe chunked form in place."""
  if isinstance(d, dict):
    for k, v in d.items():
      if isinstance(v, np.ndarray):
        if v.size * v.dtype.itemsize > MAX_CHUNK_SIZE:
          d[k] = _chunk(v)
      elif isinstance(v, dict):
        _chunk_array_leaves_in_place(v)
  elif isinstance(d, np.ndarray):
    if d.size * d.dtype.itemsize > MAX_CHUNK_SIZE:
      return _chunk(d)
  return d


def _unchunk_array_leaves_in_place(d):
  """Convert chunked array leaves back into array leaves, in place."""
  if isinstance(d, dict):
    if '__msgpack_chunked_array__' in d:
      return _unchunk(d)
    else:
      for k, v in d.items():
        if isinstance(v, dict) and '__msgpack_chunked_array__' in v:
          d[k] = _unchunk(v)
        elif isinstance(v, dict):
          _unchunk_array_leaves_in_place(v)
  return d


# User-facing API calls:


def msgpack_serialize(pytree):
  """Save data structure to bytes in msgpack format.

  Low-level function that only supports python trees with array leaves,
  for custom objects use `to_bytes`.

  Args:
    pytree: python tree of dict, list, tuple with python primitives
      and array leaves.

  Returns:
    msgpack-encoded bytes of pytree.
  """
  return msgpack.packb(pytree, default=_msgpack_ext_pack, strict_types=True)


def msgpack_restore(encoded_pytree):
  """Restore data structure from bytes in msgpack format.

  Low-level function that only supports python trees with array leaves,
  for custom objects use `from_bytes`.

  Args:
    encoded_pytree: msgpack-encoded bytes of python tree.

  Returns:
    Python tree of dict, list, tuple with python primitive
    and array leaves.
  """
  return msgpack.unpackb(
      encoded_pytree, ext_hook=_msgpack_ext_unpack, raw=False)


def from_bytes(target, encoded_bytes):
  """Restore optimizer or other object from msgpack-serialized state-dict.

  Args:
    target: template object with state-dict registrations that matches
      the structure being deserialized from `encoded_bytes`.
    encoded_bytes: msgpack serialized object structurally isomorphic to
      `target`.  Typically a flax model or optimizer.

  Returns:
    A new object structurally isomorphic to `target` containing the updated
    leaf data from saved data.
  """
  state_dict = msgpack_restore(encoded_bytes)
  state_dict = _unchunk_array_leaves_in_place(state_dict)
  return from_state_dict(target, state_dict)


def to_bytes(target):
  """Save optimizer or other object as msgpack-serialized state-dict.

  Args:
    target: template object with state-dict registrations to be
      serialized to msgpack format.  Typically a flax model or optimizer.

  Returns:
    Bytes of msgpack-encoded state-dict of `target` object.
  """
  state_dict = to_state_dict(target)
  state_dict = _np_convert_in_place(state_dict)
  state_dict = _chunk_array_leaves_in_place(state_dict)
  return msgpack_serialize(state_dict)
