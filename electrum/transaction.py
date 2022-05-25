#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2011 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.



# Note: The deserialization code originally comes from ABE.
import enum
import logging
import struct
import traceback
import sys
import io
import base64
from typing import (Sequence, Union, NamedTuple, Tuple, Optional, Iterable,
                    Callable, List, Dict, Set, TYPE_CHECKING)
from collections import defaultdict
from enum import IntEnum
import itertools
import binascii
import copy

from . import ecc, ravencoin, constants, segwit_addr, bip32, assets
from .assets import guess_asset_script_for_vin
from .bip32 import UINT32_MAX, BIP32Node
from .util import RavenValue, parse_max_spend, to_bytes, bh2u, bfh, chunks, is_hex_str, Satoshis, format_satoshis
from .ravencoin import (TYPE_ADDRESS, TYPE_SCRIPT, hash_160,
                        hash160_to_p2sh, hash160_to_p2pkh, hash_to_segwit_addr,
                        var_int, TOTAL_COIN_SUPPLY_LIMIT_IN_BTC, COIN,
                        int_to_hex, push_script, b58_address_to_hash160,
                        opcodes, add_number_to_script, base_decode, is_segwit_script_type,
                        base_encode, construct_witness, construct_script)
from .crypto import sha256d
from .logging import get_logger

if TYPE_CHECKING:
    from .wallet import Abstract_Wallet


_logger = get_logger(__name__)
DEBUG_PSBT_PARSING = False


class SerializationError(Exception):
    """ Thrown when there's a problem deserializing or serializing """


class UnknownTxinType(Exception):
    pass


class BadHeaderMagic(SerializationError):
    pass


class UnexpectedEndOfStream(SerializationError):
    pass


class PSBTInputConsistencyFailure(SerializationError):
    pass


class MalformedBitcoinScript(Exception):
    pass


class MissingTxInputAmount(Exception):
    pass


class SIGHASH(IntEnum):
    ALL = 0x01
    NONE = 0x02
    SINGLE = 0x03
    ANYONECANPAY = 0x80
    ALL_ANYONECANPAY = ALL + ANYONECANPAY
    NONE_ANYONECANPAY = NONE + ANYONECANPAY
    SINGLE_ANYONECANPAY = SINGLE + ANYONECANPAY


class TxOutput:
    scriptpubkey: bytes
    _value: Union[int, str]
    asset: Optional[str]

    def __init__(self, *, scriptpubkey: bytes, value: int, asset: str = None):
        assert isinstance(scriptpubkey, bytes)
        if isinstance(value, Satoshis):
            value = value.value
        assert isinstance(value, (str, int))
        if not (isinstance(value, int) or parse_max_spend(value) is not None):
            raise ValueError(f"bad txout value: {value!r}")
        self.scriptpubkey = scriptpubkey
        self._value = value
        self.asset = asset

    @property
    def value(self) -> Union[int, str]:
        return self._value

    @value.setter
    def value(self, value):
        assert isinstance(value, int) or parse_max_spend(value) is not None
        self._value = value

    @classmethod
    def from_address_and_value(cls, address: str, value: Union[int, str], asset: str = None) -> Union['TxOutput', 'PartialTxOutput']:
        script = bfh(ravencoin.address_to_script(address))
        if asset:
            script = assets.create_transfer_asset_script(script, asset, value)

        return cls(scriptpubkey=script,
                   value=value,
                   asset=asset)

    def serialize_to_network(self) -> bytes:
        if self.asset:
            buf = bytes(8)
        else:
            buf = self.value.to_bytes(8, byteorder="little", signed=False)
        script = self.scriptpubkey
        buf += bfh(var_int(len(script.hex()) // 2))
        buf += script
        return buf

    @classmethod
    def from_network_bytes(cls, raw: bytes) -> 'TxOutput':
        vds = BCDataStream()
        vds.write(raw)
        txout = parse_output(vds)
        if vds.can_read_more():
            raise SerializationError('extra junk at the end of TxOutput bytes')
        return txout

    def to_legacy_tuple(self) -> Tuple[int, str, RavenValue]:
        if self.asset:
            value = RavenValue(0, {self.asset: self.value})
        else:
            value = RavenValue(self.value)
        if self.address:
            return TYPE_ADDRESS, self.address, value
        return TYPE_SCRIPT, self.scriptpubkey.hex(), value

    @classmethod
    def from_legacy_tuple(cls, _type: int, addr: str, val) -> Union['TxOutput', 'PartialTxOutput']:

        if isinstance(val, Dict):
            val = RavenValue.from_json(val)
        if isinstance(val, int):
            val = RavenValue(val)

        asset_d = val.assets
        asset = None
        if asset_d:
            asset, value = list(val.assets.items())[0]
        else:
            value = val.rvn_value

        if _type == TYPE_ADDRESS:
            return cls.from_address_and_value(addr, value, asset)
        if _type == TYPE_SCRIPT:
            script = bfh(addr)
            if asset:
                script = assets.create_transfer_asset_script(script, asset, value)
            return cls(scriptpubkey=script, value=value, asset=asset)
        raise Exception(f"unexpected legacy address type: {_type}")
    
    @property
    def address(self) -> Optional[str]:
        return get_address_from_output_script(self.scriptpubkey)  # TODO cache this?

    def get_ui_address_str(self) -> str:
        addr = self.address
        if addr is not None:
            return addr
        return f"SCRIPT {self.scriptpubkey.hex()}"

    def __repr__(self):
        return f"<TxOutput script={self.scriptpubkey.hex()} address={self.address} asset={self.asset} value={self.value}>"

    def __eq__(self, other):
        if not isinstance(other, TxOutput):
            return False
        return self.scriptpubkey == other.scriptpubkey and self.value == other.value

    def __ne__(self, other):
        return not (self == other)

    def to_json(self):
        if self.asset:
            value = RavenValue(0, {self.asset: self.value})
        else:
            value = RavenValue(self.value)
        d = {
            'scriptpubkey': self.scriptpubkey.hex(),
            'address': self.address,
            'value_sats': value
        }
        return d


class BIP143SharedTxDigestFields(NamedTuple):
    hashPrevouts: str
    hashSequence: str
    hashOutputs: str


class TxOutpoint(NamedTuple):
    txid: bytes  # endianness same as hex string displayed; reverse of tx serialization order
    out_idx: int

    @classmethod
    def from_str(cls, s: str) -> 'TxOutpoint':
        hash_str, idx_str = s.split(':')
        assert len(hash_str) == 64, f"{hash_str} should be a sha256 hash"
        return TxOutpoint(txid=bfh(hash_str),
                          out_idx=int(idx_str))

    def __str__(self) -> str:
        return f"""TxOutpoint("{self.to_str()}")"""

    def __repr__(self):
        return f"<{str(self)}>"

    def to_str(self) -> str:
        return f"{self.txid.hex()}:{self.out_idx}"

    def to_json(self):
        return [self.txid.hex(), self.out_idx]

    def serialize_to_network(self) -> bytes:
        return self.txid[::-1] + bfh(int_to_hex(self.out_idx, 4))

    def is_coinbase(self) -> bool:
        return self.txid == bytes(32)


class AssetMeta(NamedTuple):
    name: str
    circulation: int
    is_owner: bool
    is_reissuable: bool
    divisions: int
    has_ipfs: bool
    ipfs_str: Optional[str]
    height: int
    div_height: Optional[int]
    ipfs_height: Optional[int]
    source_type: str  #q, r, o
    source_outpoint: TxOutpoint
    source_divisions: Optional[TxOutpoint]
    source_ipfs: Optional[TxOutpoint]


class TxInput:
    prevout: TxOutpoint
    script_sig: Optional[bytes]
    nsequence: int
    witness: Optional[bytes]
    _is_coinbase_output: bool
    sighash: Optional[int]

    def __init__(self, *,
                 prevout: TxOutpoint,
                 script_sig: bytes = None,
                 nsequence: int = 0xffffffff - 1,
                 witness: bytes = None,
                 is_coinbase_output: bool = False,
                 sighash: Optional[int] = None):
        self.prevout = prevout
        self.script_sig = script_sig
        self.nsequence = nsequence
        self.witness = witness
        self._is_coinbase_output = is_coinbase_output
        self.sighash = sighash

    @property
    def nsequence(self):
        return self._nsequence

    @nsequence.setter
    def nsequence(self, sig):
        #if self.prevout.txid.hex() == 'f619e4425cafe9e4aa211beb8c08f6529535cf37f94929c990e6366ce8dea799':
        #    traceback.print_stack()
        #    print(sig)
        
        self._nsequence = sig

    def is_coinbase_input(self) -> bool:
        """Whether this is the input of a coinbase tx."""
        return self.prevout.is_coinbase()

    def is_coinbase_output(self) -> bool:
        """Whether the coin being spent is an output of a coinbase tx.
        This matters for coin maturity.
        """
        return self._is_coinbase_output

    def value_sats(self) -> Optional[RavenValue]:
        return None

    def to_json(self):
        d = {
            'prevout_hash': self.prevout.txid.hex(),
            'prevout_n': self.prevout.out_idx,
            'coinbase': self.is_coinbase_output(),
            'nsequence': self.nsequence,
        }
        if self.script_sig is not None:
            d['scriptSig'] = self.script_sig.hex()
        if self.witness is not None:
            d['witness'] = self.witness.hex()
        return d

    def witness_elements(self)-> Sequence[bytes]:
        if not self.witness:
            return []
        vds = BCDataStream()
        vds.write(self.witness)
        n = vds.read_compact_size()
        return list(vds.read_bytes(vds.read_compact_size()) for i in range(n))

    def is_segwit(self, *, guess_for_address=False) -> bool:
        if self.witness not in (b'\x00', b'', None):
            return True
        return False


class BCDataStream(object):
    """Workalike python implementation of Bitcoin's CDataStream class."""

    def __init__(self):
        self.input = None  # type: Optional[bytearray]
        self.read_cursor = 0

    def clear(self):
        self.input = None
        self.read_cursor = 0

    def write(self, _bytes: Union[bytes, bytearray]):  # Initialize with string of _bytes
        assert isinstance(_bytes, (bytes, bytearray))
        if self.input is None:
            self.input = bytearray(_bytes)
        else:
            self.input += bytearray(_bytes)

    def read_string(self, encoding='ascii'):
        # Strings are encoded depending on length:
        # 0 to 252 :  1-byte-length followed by bytes (if any)
        # 253 to 65,535 : byte'253' 2-byte-length followed by bytes
        # 65,536 to 4,294,967,295 : byte '254' 4-byte-length followed by bytes
        # ... and the Bitcoin client is coded to understand:
        # greater than 4,294,967,295 : byte '255' 8-byte-length followed by bytes of string
        # ... but I don't think it actually handles any strings that big.
        if self.input is None:
            raise SerializationError("call write(bytes) before trying to deserialize")

        length = self.read_compact_size()

        return self.read_bytes(length).decode(encoding)

    def write_string(self, string, encoding='ascii'):
        string = to_bytes(string, encoding)
        # Length-encoded as with read-string
        self.write_compact_size(len(string))
        self.write(string)

    def read_bytes(self, length: int) -> bytes:
        if self.input is None:
            raise SerializationError("call write(bytes) before trying to deserialize")
        assert length >= 0
        input_len = len(self.input)
        read_begin = self.read_cursor
        read_end = read_begin + length
        if 0 <= read_begin <= read_end <= input_len:
            result = self.input[read_begin:read_end]  # type: bytearray
            self.read_cursor += length
            return bytes(result)
        else:
            raise SerializationError('attempt to read past end of buffer')

    def write_bytes(self, _bytes: Union[bytes, bytearray], length: int):
        assert len(_bytes) == length, len(_bytes)
        self.write(_bytes)

    def can_read_more(self) -> bool:
        if not self.input:
            return False
        return self.read_cursor < len(self.input)

    def read_boolean(self) -> bool: return self.read_bytes(1) != b'\x00'
    def read_int16(self): return self._read_num('<h')
    def read_uint16(self): return self._read_num('<H')
    def read_int32(self): return self._read_num('<i')
    def read_uint32(self): return self._read_num('<I')
    def read_int64(self): return self._read_num('<q')
    def read_uint64(self): return self._read_num('<Q')

    def write_boolean(self, val): return self.write(b'\x01' if val else b'\x00')
    def write_int16(self, val): return self._write_num('<h', val)
    def write_uint16(self, val): return self._write_num('<H', val)
    def write_int32(self, val): return self._write_num('<i', val)
    def write_uint32(self, val): return self._write_num('<I', val)
    def write_int64(self, val): return self._write_num('<q', val)
    def write_uint64(self, val): return self._write_num('<Q', val)

    def read_compact_size(self):
        try:
            size = self.input[self.read_cursor]
            self.read_cursor += 1
            if size == 253:
                size = self._read_num('<H')
            elif size == 254:
                size = self._read_num('<I')
            elif size == 255:
                size = self._read_num('<Q')
            return size
        except IndexError as e:
            raise SerializationError("attempt to read past end of buffer") from e

    def write_compact_size(self, size):
        if size < 0:
            raise SerializationError("attempt to write size < 0")
        elif size < 253:
            self.write(bytes([size]))
        elif size < 2**16:
            self.write(b'\xfd')
            self._write_num('<H', size)
        elif size < 2**32:
            self.write(b'\xfe')
            self._write_num('<I', size)
        elif size < 2**64:
            self.write(b'\xff')
            self._write_num('<Q', size)
        else:
            raise Exception(f"size {size} too large for compact_size")

    def _read_num(self, format):
        try:
            (i,) = struct.unpack_from(format, self.input, self.read_cursor)
            self.read_cursor += struct.calcsize(format)
        except Exception as e:
            raise SerializationError(e) from e
        return i

    def _write_num(self, format, num):
        s = struct.pack(format, num)
        self.write(s)


def script_GetOp(_bytes : bytes):
    i = 0
    while i < len(_bytes):
        vch = None
        opcode = _bytes[i]
        i += 1

        if opcode <= opcodes.OP_PUSHDATA4:
            nSize = opcode
            if opcode == opcodes.OP_PUSHDATA1:
                try: nSize = _bytes[i]
                except IndexError: raise MalformedBitcoinScript()
                i += 1
            elif opcode == opcodes.OP_PUSHDATA2:
                try: (nSize,) = struct.unpack_from('<H', _bytes, i)
                except struct.error: raise MalformedBitcoinScript()
                i += 2
            elif opcode == opcodes.OP_PUSHDATA4:
                try: (nSize,) = struct.unpack_from('<I', _bytes, i)
                except struct.error: raise MalformedBitcoinScript()
                i += 4
            vch = _bytes[i:i + nSize]
            i += nSize

        yield opcode, vch, i


class OPPushDataGeneric:
    def __init__(self, pushlen: Callable=None):
        if pushlen is not None:
            self.check_data_len = pushlen

    @classmethod
    def check_data_len(cls, datalen: int) -> bool:
        # Opcodes below OP_PUSHDATA4 all just push data onto stack, and are equivalent.
        return opcodes.OP_PUSHDATA4 >= datalen >= 0

    @classmethod
    def is_instance(cls, item):
        # accept objects that are instances of this class
        # or other classes that are subclasses
        return isinstance(item, cls) \
               or (isinstance(item, type) and issubclass(item, cls))

class OPGeneric:
    def __init__(self, matcher: Callable=None):
        if matcher is not None:
            self.matcher = matcher

    def match(self, op) -> bool:
        return self.matcher(op)

    @classmethod
    def is_instance(cls, item):
        # accept objects that are instances of this class
        # or other classes that are subclasses
        return isinstance(item, cls) \
               or (isinstance(item, type) and issubclass(item, cls))

class OPGeneric:
    def __init__(self, matcher: Callable=None):
        if matcher is not None:
            self.matcher = matcher

    def match(self, op) -> bool:
        return self.matcher(op)

    @classmethod
    def is_instance(cls, item):
        # accept objects that are instances of this class
        # or other classes that are subclasses
        return isinstance(item, cls) \
               or (isinstance(item, type) and issubclass(item, cls))

OPPushDataPubkey = OPPushDataGeneric(lambda x: x in (33, 65))
OP_ANYSEGWIT_VERSION = OPGeneric(lambda x: x in list(range(opcodes.OP_1, opcodes.OP_16 + 1)))

SCRIPTPUBKEY_TEMPLATE_P2PK = [OPPushDataGeneric(lambda x: x in (33, 65)), opcodes.OP_CHECKSIG]
SCRIPTPUBKEY_TEMPLATE_P2PKH = [opcodes.OP_DUP, opcodes.OP_HASH160,
                               OPPushDataGeneric(lambda x: x == 20),
                               opcodes.OP_EQUALVERIFY, opcodes.OP_CHECKSIG]
SCRIPTPUBKEY_TEMPLATE_P2SH = [opcodes.OP_HASH160, OPPushDataGeneric(lambda x: x == 20), opcodes.OP_EQUAL]
SCRIPTPUBKEY_TEMPLATE_WITNESS_V0 = [opcodes.OP_0, OPPushDataGeneric(lambda x: x in (20, 32))]
SCRIPTPUBKEY_TEMPLATE_P2WPKH = [opcodes.OP_0, OPPushDataGeneric(lambda x: x == 20)]
SCRIPTPUBKEY_TEMPLATE_P2WSH = [opcodes.OP_0, OPPushDataGeneric(lambda x: x == 32)]
SCRIPTPUBKEY_TEMPLATE_ANYSEGWIT = [OP_ANYSEGWIT_VERSION, OPPushDataGeneric(lambda x: x in list(range(2, 40 + 1)))]

def check_scriptpubkey_template_and_dust(scriptpubkey, amount: Optional[int]):
    if match_script_against_template(scriptpubkey, SCRIPTPUBKEY_TEMPLATE_P2PKH):
        dust_limit = ravencoin.DUST_LIMIT_P2PKH
    elif match_script_against_template(scriptpubkey, SCRIPTPUBKEY_TEMPLATE_P2SH):
        dust_limit = ravencoin.DUST_LIMIT_P2SH
    elif match_script_against_template(scriptpubkey, SCRIPTPUBKEY_TEMPLATE_P2WSH):
        dust_limit = ravencoin.DUST_LIMIT_P2WSH
    elif match_script_against_template(scriptpubkey, SCRIPTPUBKEY_TEMPLATE_P2WPKH):
        dust_limit = ravencoin.DUST_LIMIT_P2WPKH
    else:
        raise Exception(f'scriptpubkey does not conform to any template: {scriptpubkey.hex()}')
    if amount < dust_limit:
        raise Exception(f'amount ({amount}) is below dust limit for scriptpubkey type ({dust_limit})')


def match_script_against_template(script, template, debug=False) -> bool:
    """Returns whether 'script' matches 'template'."""
    if script is None:
        return False
    # optionally decode script now:
    if isinstance(script, (bytes, bytearray)):
        try:
            script = [x for x in script_GetOp(script)]
        except MalformedBitcoinScript:
            if debug:
                _logger.debug(f"malformed script")
            return False

    # Chop off assets
    op_rvn_asset = len(script)
    for i in range(len(script)):
        # print(f'Checking OPCODE {script_item[0]} {int(opcodes.OP_RVN_ASSET)}')
        if script[i][0] == int(opcodes.OP_RVN_ASSET): # Don't check past op RVN asset
            op_rvn_asset = i
            break
    script = script[:op_rvn_asset]
    
    if debug:
        _logger.debug(f"match script against template: {script}")
    if len(script) != len(template):
        if debug:
            _logger.debug(f"length mismatch {len(script)} != {len(template)}")
        return False
    for i in range(len(script)):
        template_item = template[i]
        script_item = script[i]
       
        if OPPushDataGeneric.is_instance(template_item) and template_item.check_data_len(script_item[0]):
            continue
        if OPGeneric.is_instance(template_item) and template_item.match(script_item[0]):
            continue
        if template_item != script_item[0]:
            if debug:
                _logger.debug(f"item mismatch at position {i}: {template_item} != {script_item[0]}")
            return False
    return True


def get_script_type_from_output_script(_bytes: bytes) -> Optional[str]:
    if _bytes is None:
        return None
    try:
        decoded = [x for x in script_GetOp(_bytes)]
    except MalformedBitcoinScript:
        return None
    if match_script_against_template(decoded, SCRIPTPUBKEY_TEMPLATE_P2PKH):
        return 'p2pkh'
    if match_script_against_template(decoded, SCRIPTPUBKEY_TEMPLATE_P2SH):
        return 'p2sh'
    if match_script_against_template(decoded, SCRIPTPUBKEY_TEMPLATE_P2WPKH):
        return 'p2wpkh'
    if match_script_against_template(decoded, SCRIPTPUBKEY_TEMPLATE_P2WSH):
        return 'p2wsh'
    if match_script_against_template(decoded, SCRIPTPUBKEY_TEMPLATE_P2PK):
        return 'p2pk'
    return None


def is_output_script_p2pk(_bytes: bytes) -> bool:
    try:
        raw_decoded = [x for x in script_GetOp(_bytes)]
    except MalformedBitcoinScript:
        return False

    decoded = []
    for tup in raw_decoded:
        if tup[0] == opcodes.OP_RVN_ASSET:
            break
        decoded.append(tup)

    # p2pk (deprecated)
    if match_script_against_template(decoded, SCRIPTPUBKEY_TEMPLATE_P2PK):
        return True
    return False


def is_asset_output_script_malformed_or_non_standard(_bytes: bytes) -> bool:
    try:
        raw_decoded = [x for x in script_GetOp(_bytes)]
    except MalformedBitcoinScript:
        return True

    decoded = []
    record = False
    for tup in raw_decoded:
        if tup[0] == opcodes.OP_RVN_ASSET:
            record = True
        if record:
            decoded.append(tup)

    asset_portion = BCDataStream()
    try:
        asset_portion.write(decoded[1][1])
        assert asset_portion.read_bytes(3) == b'rvn'
        script_type = asset_portion.read_bytes(1)
        asset_name_len = asset_portion.read_bytes(1)[0]
        asset_name = asset_portion.read_bytes(asset_name_len)
        assert len(asset_name) == asset_name_len
        if script_type != b'o':
            asset_portion.read_int64()
            # We store reissues & restricted assets
            if script_type == b'q':
                if asset_portion.read_bytes(3)[2] == 1:
                    asset_portion.read_bytes(34)
            elif script_type == b'r':
                asset_portion.read_bytes(2)
                if asset_portion.can_read_more():
                    asset_portion.read_bytes(34)
            elif script_type == b't':
                pass
                # We cannot know what the ipfs message is
                # if asset_portion.can_read_more():
                #    asset_portion.read_bytes(34)
            else:
                return True
        if asset_portion.can_read_more():
            return True
    except:
        return True
    return False


def get_address_from_output_script(_bytes: bytes, *, net=None) -> Optional[str]:
    try:
        raw_decoded = [x for x in script_GetOp(_bytes)]
    except MalformedBitcoinScript:
        return None

    decoded = []
    for tup in raw_decoded:
        if tup[0] == opcodes.OP_RVN_ASSET:
            break
        decoded.append(tup)

    # p2pk (deprecated)
    if match_script_against_template(decoded, SCRIPTPUBKEY_TEMPLATE_P2PK):
        pubkey_bytes = decoded[0][1]
        h160 = hash_160(pubkey_bytes)
        return hash160_to_p2pkh(h160, net=net)

    # p2pkh
    if match_script_against_template(decoded, SCRIPTPUBKEY_TEMPLATE_P2PKH):
        return hash160_to_p2pkh(decoded[2][1], net=net)

    # p2sh
    if match_script_against_template(decoded, SCRIPTPUBKEY_TEMPLATE_P2SH):
        return hash160_to_p2sh(decoded[1][1], net=net)

    # segwit address (version 0)
    if match_script_against_template(decoded, SCRIPTPUBKEY_TEMPLATE_WITNESS_V0):
        return hash_to_segwit_addr(decoded[1][1], witver=0, net=net)

    # segwit address (version 1-16)
    future_witness_versions = list(range(opcodes.OP_1, opcodes.OP_16 + 1))
    for witver, opcode in enumerate(future_witness_versions, start=1):
        match = [opcode, OPPushDataGeneric(lambda x: 2 <= x <= 40)]
        if match_script_against_template(decoded, match):
            return hash_to_segwit_addr(decoded[1][1], witver=witver, net=net)

    return None


def parse_input(vds: BCDataStream) -> TxInput:
    prevout_hash = vds.read_bytes(32)[::-1]
    prevout_n = vds.read_uint32()
    prevout = TxOutpoint(txid=prevout_hash, out_idx=prevout_n)
    script_sig = vds.read_bytes(vds.read_compact_size())
    nsequence = vds.read_uint32()

    # Calculate the sig hash type

    sigtype = None
    try:
        # Theoretically the script_sig is the very end of the first stack push
        sigtype = next(iter(script_GetOp(script_sig)))[1][-1]
        if sigtype not in list(map(int, SIGHASH)):
            raise Exception("invalid sighash: {}".format(sigtype))
    except Exception:
        sigtype = None

    return TxInput(prevout=prevout, script_sig=script_sig, nsequence=nsequence, sighash=sigtype)


def parse_witness(vds: BCDataStream, txin: TxInput) -> None:
    n = vds.read_compact_size()
    witness_elements = list(vds.read_bytes(vds.read_compact_size()) for i in range(n))
    txin.witness = bfh(construct_witness(witness_elements))


def get_assets_from_script(script: bytes) -> Dict[str, int]:

    # TODO: Generalize

    def search_for_rvn(b: bytes, start: int) -> int:
        index = -1
        if b[start:start+3] == b'rvn':
            index = start+3
        elif b[start+1:start+4] == b'rvn':
            index = start+4
        return index

    if script[0] == 0xA9 and script[1] == 0x14 and script[22] == 0x87:  # Script hash
        index = search_for_rvn(script, 25)
    else:  # Assumed Pubkey hash
        index = search_for_rvn(script, 27)

    if index > 0:
        type = script[index]
        asset_name_len = script[index+1]
        asset_name = script[index+2:index+2+asset_name_len]
        if type != b'o'[0]:
            sat_amt = int.from_bytes(script[index+2+asset_name_len:index+10+asset_name_len], byteorder='little')
        else:  # Give a value of '1' to ownership tokens
            sat_amt = 100_000_000
        name = asset_name.decode('ascii')
        return {name: sat_amt}
    else:
        return {}


def parse_output(vds: BCDataStream) -> TxOutput:
    value = vds.read_int64()
    if value > TOTAL_COIN_SUPPLY_LIMIT_IN_BTC * COIN:
        raise SerializationError('invalid output amount (too large)')
    if value < 0:
        raise SerializationError('invalid output amount (negative)')
    value = Satoshis(value)
    scriptpubkey = vds.read_bytes(vds.read_compact_size())
    assets = get_assets_from_script(scriptpubkey)
    asset = None
    if assets:
        asset, value = list(assets.items())[0]

    return TxOutput(value=value, asset=asset, scriptpubkey=scriptpubkey)


# pay & redeem scripts

def multisig_script(public_keys: Sequence[str], m: int) -> str:
    n = len(public_keys)
    assert 1 <= m <= n <= 15, f'm {m}, n {n}'
    return construct_script([m, *public_keys, n, opcodes.OP_CHECKMULTISIG])


class Transaction:
    _cached_network_ser: Optional[str]
    for_swap = False

    def __str__(self):
        return self.serialize()

    def __deepcopy__(self, memo):
        cls = self.__class__
        result = cls.__new__(cls)
        memo[id(self)] = result
        for k, v in self.__dict__.items():
            if k != '_wallet':
                setattr(result, k, copy.deepcopy(v))
            else:
                setattr(result, k, v)
        return result

    def __init__(self, raw, wallet: 'Abstract_Wallet' = None):
        self._wallet = wallet
        if raw is None:
            self._cached_network_ser = None
        elif isinstance(raw, str):
            self._cached_network_ser = raw.strip() if raw else None
            assert is_hex_str(self._cached_network_ser)
        elif isinstance(raw, (bytes, bytearray)):
            self._cached_network_ser = bh2u(raw)
        else:
            raise Exception(f"cannot initialize transaction from {raw}")
        self._inputs = None  # type: List[TxInput]
        self._outputs = None  # type: List[TxOutput]
        self._locktime = 0
        self._version = 2

        self._cached_txid = None  # type: Optional[str]

    @property
    def locktime(self):
        self.deserialize()
        return self._locktime

    @locktime.setter
    def locktime(self, value: int):
        assert isinstance(value, int), f"locktime must be int, not {value!r}"
        # Assume we have the correct locktime for SIGHASH_SINGLE
        if self.for_swap:
            return
        #if value != 0:
        #    traceback.print_stack()
        #    print(value)
        self._locktime = value
        self.invalidate_ser_cache()

    @property
    def version(self):
        self.deserialize()
        return self._version

    @version.setter
    def version(self, value):
        self._version = value
        self.invalidate_ser_cache()

    def to_json(self) -> dict:
        d = {
            'version': self.version,
            'locktime': self.locktime,
            'inputs': [txin.to_json() for txin in self.inputs()],
            'outputs': [txout.to_json() for txout in self.outputs()],
            'swap': self.for_swap
        }
        return d

    def inputs(self) -> Sequence[TxInput]:
        if self._inputs is None:
            self.deserialize()
        return self._inputs

    def outputs(self) -> Sequence[TxOutput]:
        if self._outputs is None:
            self.deserialize()
        return self._outputs

    def deserialize(self) -> None:
        if self._cached_network_ser is None:
            return
        if self._inputs is not None:
            return

        raw_bytes = bfh(self._cached_network_ser)
        vds = BCDataStream()
        vds.write(raw_bytes)
        self._version = vds.read_int32()
        n_vin = vds.read_compact_size()
        is_segwit = (n_vin == 0)
        if is_segwit:
            marker = vds.read_bytes(1)
            if marker != b'\x01':
                raise ValueError('invalid txn marker byte: {}'.format(marker))
            n_vin = vds.read_compact_size()
        if n_vin < 1:
            raise SerializationError('tx needs to have at least 1 input')
        self._inputs = [parse_input(vds) for i in range(n_vin)]
        n_vout = vds.read_compact_size()
        if n_vout < 1:
            raise SerializationError('tx needs to have at least 1 output')
        self._outputs = [parse_output(vds) for i in range(n_vout)]
        if is_segwit:
            for txin in self._inputs:
                parse_witness(vds, txin)
        self._locktime = vds.read_uint32()
        if vds.can_read_more():
            raise SerializationError('extra junk at the end')

    @classmethod
    def get_siglist(self, txin: 'PartialTxInput', *, estimate_size=False):
        if txin.is_coinbase_input():
            return [], []

        if estimate_size:
            try:
                pubkey_size = len(txin.pubkeys[0])
            except IndexError:
                pubkey_size = 33  # guess it is compressed
            num_pubkeys = max(1, len(txin.pubkeys))
            pk_list = ["00" * pubkey_size] * num_pubkeys
            num_sig = max(1, txin.num_sig)
            # we guess that signatures will be 72 bytes long
            # note: DER-encoded ECDSA signatures are 71 or 72 bytes in practice
            #       See https://bitcoin.stackexchange.com/questions/77191/what-is-the-maximum-size-of-a-der-encoded-ecdsa-signature
            #       We assume low S (as that is a bitcoin standardness rule).
            #       We do not assume low R (even though the sigs we create conform), as external sigs,
            #       e.g. from a hw signer cannot be expected to have a low R.
            sig_list = ["00" * 72] * num_sig
        else:
            pk_list = [pubkey.hex() for pubkey in txin.pubkeys]
            sig_list = [txin.part_sigs.get(pubkey, b'').hex() for pubkey in txin.pubkeys]
            if txin.is_complete():
                sig_list = [sig for sig in sig_list if sig]
        return pk_list, sig_list

    @classmethod
    def serialize_witness(cls, txin: TxInput, *, estimate_size=False) -> str:
        if txin.witness is not None:
            return txin.witness.hex()
        if txin.is_coinbase_input():
            return ''
        assert isinstance(txin, PartialTxInput)

        _type = txin.script_type
        if not txin.is_segwit():
            return construct_witness([])

        if estimate_size and txin.witness_sizehint is not None:
            return '00' * txin.witness_sizehint
        if _type in ('address', 'unknown') and estimate_size:
            _type = cls.guess_txintype_from_address(txin.address)
        pubkeys, sig_list = cls.get_siglist(txin, estimate_size=estimate_size)
        if _type in ['p2wpkh', 'p2wpkh-p2sh']:
            return construct_witness([sig_list[0], pubkeys[0]])
        elif _type in ['p2wsh', 'p2wsh-p2sh']:
            witness_script = multisig_script(pubkeys, txin.num_sig)
            return construct_witness([0, *sig_list, witness_script])
        elif _type in ['p2pk', 'p2pkh', 'p2sh']:
            return construct_witness([])
        raise UnknownTxinType(f'cannot construct witness for txin_type: {_type}')

    @classmethod
    def guess_txintype_from_address(cls, addr: Optional[str]) -> str:
        # It's not possible to tell the script type in general
        # just from an address.
        # - "1" addresses are of course p2pkh
        # - "3" addresses are p2sh but we don't know the redeem script..
        # - "bc1" addresses (if they are 42-long) are p2wpkh
        # - "bc1" addresses that are 62-long are p2wsh but we don't know the script..
        # If we don't know the script, we _guess_ it is pubkeyhash.
        # As this method is used e.g. for tx size estimation,
        # the estimation will not be precise.
        if addr is None:
            return 'p2wpkh'
        witver, witprog = segwit_addr.decode_segwit_address(constants.net.SEGWIT_HRP, addr)
        if witprog is not None:
            return 'p2wpkh'
        addrtype, hash_160_ = b58_address_to_hash160(addr)
        if addrtype == constants.net.ADDRTYPE_P2PKH:
            return 'p2pkh'
        elif addrtype == constants.net.ADDRTYPE_P2SH:
            return 'p2wpkh-p2sh'
        raise Exception(f'unrecognized address: {repr(addr)}')

    @classmethod
    def input_script(self, txin: TxInput, *, estimate_size=False) -> str:
        # This is for the hashs; don't need to calculate asset outs here

        if txin.script_sig is not None:
            return txin.script_sig.hex()
        if txin.is_coinbase_input():
            return ''
        assert isinstance(txin, PartialTxInput)

        if txin.is_p2sh_segwit() and txin.redeem_script:
            return construct_script([txin.redeem_script])
        if txin.is_native_segwit():
            return ''

        _type = txin.script_type
        pubkeys, sig_list = self.get_siglist(txin, estimate_size=estimate_size)
        if _type in ('address', 'unknown') and estimate_size:
            _type = self.guess_txintype_from_address(txin.address)
        if _type == 'p2pk':
            script = construct_script([sig_list[0]])
        elif _type == 'p2sh':
            # put op_0 before script
            redeem_script = multisig_script(pubkeys, txin.num_sig)
            script =  construct_script([0, *sig_list, redeem_script])
        elif _type == 'p2pkh':
            script = construct_script([sig_list[0], pubkeys[0]])
        elif _type in ['p2wpkh', 'p2wsh']:
            script = ''
        elif _type == 'p2wpkh-p2sh':
            raise NotImplementedError()
            redeem_script = ravencoin.p2wpkh_nested_script(pubkeys[0])
            script = construct_script([redeem_script])
        elif _type == 'p2wsh-p2sh':
            raise NotImplementedError()
            if estimate_size:
                witness_script = ''
            else:
                witness_script = self.get_preimage_script(txin)
            redeem_script = ravencoin.p2wsh_nested_script(witness_script)
            script = construct_script([redeem_script])
        else:
            raise UnknownTxinType(f'cannot construct scriptSig for txin_type: {_type} {txin.scriptpubkey}')

        return script

    @classmethod
    def get_preimage_script(cls, txin: 'PartialTxInput', wallet: 'Abstract_Wallet' = None, txin_locking_script_overrides: Dict = None) -> str:
        if txin.witness_script:
            if opcodes.OP_CODESEPARATOR in [x[0] for x in script_GetOp(txin.witness_script)]:
                raise Exception('OP_CODESEPARATOR black magic is not supported')
            raise NotImplementedError()
            # return txin.witness_script.hex()
        if not txin.is_segwit() and txin.redeem_script:
            if opcodes.OP_CODESEPARATOR in [x[0] for x in script_GetOp(txin.redeem_script)]:
                raise Exception('OP_CODESEPARATOR black magic is not supported')
            return txin.redeem_script.hex()

        pubkeys = [pk.hex() for pk in txin.pubkeys]
        if txin.script_type in ['p2sh', 'p2wsh', 'p2wsh-p2sh']:
            script = multisig_script(pubkeys, txin.num_sig)
        elif txin.script_type in ['p2pkh', 'p2wpkh', 'p2wpkh-p2sh']:
            pubkey = pubkeys[0]
            pkh = bh2u(hash_160(bfh(pubkey)))
            script = ravencoin.pubkeyhash_to_p2pkh_script(pkh)
        elif txin.script_type == 'p2pk':
            pubkey = pubkeys[0]
            script = ravencoin.public_key_to_p2pk_script(pubkey)
        else:
            raise UnknownTxinType(f'cannot construct preimage_script for txin_type: {txin.script_type}')

        a = txin.value_sats().assets
        if a:
            asset, amt = list(a.items())[0]
            script = guess_asset_script_for_vin(bfh(script), asset, amt, txin, wallet)
        if wallet:
            script = wallet.get_nonstandard_outpoints().get(txin.prevout.to_str(), script)
        if txin_locking_script_overrides:
            _logger.debug('Trying to override')
            script = txin_locking_script_overrides.get(txin.prevout.to_str(), script)

        return script

    @classmethod
    def serialize_input(self, txin: TxInput, script: str) -> str:
        # Prev hash and index
        s = txin.prevout.serialize_to_network().hex()
        # Script length, script, sequence
        s += var_int(len(script)//2)
        s += script
        s += int_to_hex(txin.nsequence, 4)
        return s

    def _calc_bip143_shared_txdigest_fields(self) -> BIP143SharedTxDigestFields:
        inputs = self.inputs()
        outputs = self.outputs()
        hashPrevouts = bh2u(sha256d(b''.join(txin.prevout.serialize_to_network() for txin in inputs)))
        hashSequence = bh2u(sha256d(bfh(''.join(int_to_hex(txin.nsequence, 4) for txin in inputs))))
        hashOutputs = bh2u(sha256d(bfh(''.join(o.serialize_to_network().hex() for o in outputs))))
        return BIP143SharedTxDigestFields(hashPrevouts=hashPrevouts,
                                          hashSequence=hashSequence,
                                          hashOutputs=hashOutputs)

    def is_segwit(self, *, guess_for_address=False):
        return any(txin.is_segwit(guess_for_address=guess_for_address)
                   for txin in self.inputs())

    def invalidate_ser_cache(self):
        self._cached_network_ser = None
        self._cached_txid = None

    def serialize(self) -> str:
        if not self._cached_network_ser:
            self._cached_network_ser = self.serialize_to_network(estimate_size=False, include_sigs=True)
        return self._cached_network_ser

    def serialize_as_bytes(self) -> bytes:
        return bfh(self.serialize())

    def serialize_to_network(self, *, estimate_size=False, include_sigs=True, force_legacy=False) -> str:
        """Serialize the transaction as used on the Bitcoin network, into hex.
        `include_sigs` signals whether to include scriptSigs and witnesses.
        `force_legacy` signals to use the pre-segwit format
        note: (not include_sigs) implies force_legacy
        """
        self.deserialize()
        nVersion = int_to_hex(self.version, 4)
        nLocktime = int_to_hex(self.locktime, 4)
        inputs = self.inputs()
        outputs = self.outputs()

        def create_script_sig(txin: TxInput) -> str:
            if include_sigs:
                return self.input_script(txin, estimate_size=estimate_size)
            return ''
        txins = var_int(len(inputs)) + ''.join(self.serialize_input(txin, create_script_sig(txin))
                                               for txin in inputs)
        txouts = var_int(len(outputs)) + ''.join(o.serialize_to_network().hex() for o in outputs)

        use_segwit_ser_for_estimate_size = estimate_size and self.is_segwit(guess_for_address=True)
        use_segwit_ser_for_actual_use = not estimate_size and self.is_segwit()
        use_segwit_ser = use_segwit_ser_for_estimate_size or use_segwit_ser_for_actual_use
        if include_sigs and not force_legacy and use_segwit_ser:
            marker = '00'
            flag = '01'
            witness = ''.join(self.serialize_witness(x, estimate_size=estimate_size) for x in inputs)
            return nVersion + marker + flag + txins + txouts + witness + nLocktime
        else:
            return nVersion + txins + txouts + nLocktime

    def to_qr_data(self) -> str:
        """Returns tx as data to be put into a QR code. No side-effects."""
        tx = copy.deepcopy(self)  # make copy as we mutate tx
        if isinstance(tx, PartialTransaction):
            # this makes QR codes a lot smaller (or just possible in the first place!)
            tx.convert_all_utxos_to_witness_utxos()
        tx_bytes = tx.serialize_as_bytes()
        return base_encode(tx_bytes, base=43)

    def txid(self) -> Optional[str]:
        if self._cached_txid is None:
            self.deserialize()
            all_segwit = all(txin.is_segwit() for txin in self.inputs())
            if not all_segwit and not self.is_complete():
                return None
            try:
                ser = self.serialize_to_network(force_legacy=True)
            except UnknownTxinType:
                # we might not know how to construct scriptSig for some scripts
                return None
            self._cached_txid = bh2u(sha256d(bfh(ser))[::-1])
        return self._cached_txid

    def wtxid(self) -> Optional[str]:
        self.deserialize()
        if not self.is_complete():
            return None
        try:
            ser = self.serialize_to_network()
        except UnknownTxinType:
            # we might not know how to construct scriptSig/witness for some scripts
            return None
        return bh2u(sha256d(bfh(ser))[::-1])

    def add_info_from_wallet(self, wallet: 'Abstract_Wallet', **kwargs) -> None:
        return  # no-op

    def is_final(self) -> bool:
        """Whether RBF is disabled."""
        return not any([txin.nsequence < 0xffffffff - 1 for txin in self.inputs()])

    def estimated_size(self):
        """Return an estimated virtual tx size in vbytes.
        BIP-0141 defines 'Virtual transaction size' to be weight/4 rounded up.
        This definition is only for humans, and has little meaning otherwise.
        If we wanted sub-byte precision, fee calculation should use transaction
        weights, but for simplicity we approximate that with (virtual_size)x4
        """
        weight = self.estimated_weight()
        return self.virtual_size_from_weight(weight)

    @classmethod
    def estimated_input_weight(cls, txin, is_segwit_tx):
        '''Return an estimate of serialized input weight in weight units.'''
        script = cls.input_script(txin, estimate_size=True)
        input_size = len(cls.serialize_input(txin, script)) // 2

        if txin.is_segwit(guess_for_address=True):
            witness_size = len(cls.serialize_witness(txin, estimate_size=True)) // 2
        else:
            witness_size = 1 if is_segwit_tx else 0

        return 4 * input_size + witness_size

    @classmethod
    def estimated_output_size_for_address(cls, address: str) -> int:
        """Return an estimate of serialized output size in bytes."""
        script = ravencoin.address_to_script(address)
        return cls.estimated_output_size_for_script(script)

    @classmethod
    def estimated_output_size_for_address_with_asset(cls, address: str, asset: str) -> int:
        """Return an estimate of serialized output size in bytes."""
        script = ravencoin.address_to_script(address)
        est_raw = cls.estimated_output_size_for_script(script)
        return est_raw + 1 + 1 + 3 + 1 + 1 + len(asset) + 8 + 1

    @classmethod
    def estimated_output_size_for_script(cls, script: str) -> int:
        """Return an estimate of serialized output size in bytes."""
        # 8 byte value + varint script len + script
        script_len = len(script) // 2
        var_int_len = len(var_int(script_len)) // 2
        return 8 + var_int_len + script_len

    @classmethod
    def virtual_size_from_weight(cls, weight):
        return weight // 4 + (weight % 4 > 0)

    @classmethod
    def satperbyte_from_satperkw(cls, feerate_kw):
        """Converts feerate from sat/kw to sat/vbyte."""
        return feerate_kw * 4 / 1000

    def estimated_total_size(self):
        """Return an estimated total transaction size in bytes."""
        if not self.is_complete() or self._cached_network_ser is None:
            return len(self.serialize_to_network(estimate_size=True)) // 2
        else:
            return len(self._cached_network_ser) // 2  # ASCII hex string

    def estimated_witness_size(self):
        """Return an estimate of witness size in bytes."""
        estimate = not self.is_complete()
        if not self.is_segwit(guess_for_address=estimate):
            return 0
        inputs = self.inputs()
        witness = ''.join(self.serialize_witness(x, estimate_size=estimate) for x in inputs)
        witness_size = len(witness) // 2 + 2  # include marker and flag
        return witness_size

    def estimated_base_size(self):
        """Return an estimated base transaction size in bytes."""
        return self.estimated_total_size() - self.estimated_witness_size()

    def estimated_weight(self):
        """Return an estimate of transaction weight."""
        total_tx_size = self.estimated_total_size()
        base_tx_size = self.estimated_base_size()
        return 3 * base_tx_size + total_tx_size

    def is_complete(self) -> bool:
        return True

    def get_output_idxs_from_scriptpubkey(self, script: str) -> Set[int]:
        """Returns the set indices of outputs with given script."""
        assert isinstance(script, str)  # hex
        # build cache if there isn't one yet
        # note: can become stale and return incorrect data
        #       if the tx is modified later; that's out of scope.
        if not hasattr(self, '_script_to_output_idx'):
            d = defaultdict(set)
            for output_idx, o in enumerate(self.outputs()):
                o_script = o.scriptpubkey.hex()
                assert isinstance(o_script, str)
                d[o_script].add(output_idx)
            self._script_to_output_idx = d
        return set(self._script_to_output_idx[script])  # copy

    def get_output_idxs_from_address(self, addr: str) -> Set[int]:
        script = ravencoin.address_to_script(addr)
        return self.get_output_idxs_from_scriptpubkey(script)

    def output_value_for_address(self, addr):
        # assumes exactly one output has that address
        for o in self.outputs():
            if o.address == addr:
                return o.value
        else:
            raise Exception('output not found', addr)

    def get_input_idx_that_spent_prevout(self, prevout: TxOutpoint) -> Optional[int]:
        # build cache if there isn't one yet
        # note: can become stale and return incorrect data
        #       if the tx is modified later; that's out of scope.
        if not hasattr(self, '_prevout_to_input_idx'):
            d = {}  # type: Dict[TxOutpoint, int]
            for i, txin in enumerate(self.inputs()):
                d[txin.prevout] = i
            self._prevout_to_input_idx = d
        idx = self._prevout_to_input_idx.get(prevout)
        if idx is not None:
            assert self.inputs()[idx].prevout == prevout
        return idx


def convert_raw_tx_to_hex(raw: Union[str, bytes]) -> str:
    """Sanitizes tx-describing input (hex/base43/base64) into
    raw tx hex string."""
    if not raw:
        raise ValueError("empty string")
    raw_unstripped = raw
    raw = raw.strip()
    # try hex
    try:
        return binascii.unhexlify(raw).hex()
    except:
        pass
    # try base43
    try:
        return base_decode(raw, base=43).hex()
    except:
        pass
    # try base64
    if raw[0:6] in ('cHNidP', b'cHNidP'):  # base64 psbt
        try:
            return base64.b64decode(raw).hex()
        except:
            pass
    # raw bytes (do not strip whitespaces in this case)
    if isinstance(raw_unstripped, bytes):
        return raw_unstripped.hex()
    raise ValueError(f"failed to recognize transaction encoding for txt: {raw[:30]}...")


def tx_from_any(raw: Union[str, bytes], *,
                deserialize: bool = True) -> Union['PartialTransaction', 'Transaction']:
    if isinstance(raw, bytearray):
        raw = bytes(raw)
    raw = convert_raw_tx_to_hex(raw)
    try:
        return PartialTransaction.from_raw_psbt(raw)
    except BadHeaderMagic:
        if raw[:10] == b'EPTF\xff'.hex():
            raise SerializationError("Partial transactions generated with old Electrum versions "
                                     "(< 4.0) are no longer supported. Please upgrade Electrum on "
                                     "the other machine where this transaction was created.")
    try:
        tx = Transaction(raw)
        if deserialize:
            tx.deserialize()
        return tx
    except Exception as e:
        raise SerializationError(f"Failed to recognise tx encoding, or to parse transaction. "
                                 f"raw: {raw[:30]}...") from e


class PSBTGlobalType(IntEnum):
    UNSIGNED_TX = 0
    XPUB = 1
    VERSION = 0xFB


class PSBTInputType(IntEnum):
    NON_WITNESS_UTXO = 0
    WITNESS_UTXO = 1
    PARTIAL_SIG = 2
    SIGHASH_TYPE = 3
    REDEEM_SCRIPT = 4
    WITNESS_SCRIPT = 5
    BIP32_DERIVATION = 6
    FINAL_SCRIPTSIG = 7
    FINAL_SCRIPTWITNESS = 8


class PSBTOutputType(IntEnum):
    REDEEM_SCRIPT = 0
    WITNESS_SCRIPT = 1
    BIP32_DERIVATION = 2


# Serialization/deserialization tools
def deser_compact_size(f) -> Optional[int]:
    try:
        nit = f.read(1)[0]
    except IndexError:
        return None     # end of file

    if nit == 253:
        nit = struct.unpack("<H", f.read(2))[0]
    elif nit == 254:
        nit = struct.unpack("<I", f.read(4))[0]
    elif nit == 255:
        nit = struct.unpack("<Q", f.read(8))[0]
    return nit


class PSBTSection:

    def _populate_psbt_fields_from_fd(self, fd=None):
        if not fd: return

        while True:
            try:
                key_type, key, val = self.get_next_kv_from_fd(fd)
            except StopIteration:
                break
            self.parse_psbt_section_kv(key_type, key, val)

    @classmethod
    def get_next_kv_from_fd(cls, fd) -> Tuple[int, bytes, bytes]:
        key_size = deser_compact_size(fd)
        if key_size == 0:
            raise StopIteration()
        if key_size is None:
            raise UnexpectedEndOfStream()

        full_key = fd.read(key_size)
        key_type, key = cls.get_keytype_and_key_from_fullkey(full_key)

        val_size = deser_compact_size(fd)
        if val_size is None: raise UnexpectedEndOfStream()
        val = fd.read(val_size)

        return key_type, key, val

    @classmethod
    def create_psbt_writer(cls, fd):
        def wr(key_type: int, val: bytes, key: bytes = b''):
            full_key = cls.get_fullkey_from_keytype_and_key(key_type, key)
            fd.write(bytes.fromhex(var_int(len(full_key))))  # key_size
            fd.write(full_key)  # key
            fd.write(bytes.fromhex(var_int(len(val))))  # val_size
            fd.write(val)  # val
        return wr

    @classmethod
    def get_keytype_and_key_from_fullkey(cls, full_key: bytes) -> Tuple[int, bytes]:
        with io.BytesIO(full_key) as key_stream:
            key_type = deser_compact_size(key_stream)
            if key_type is None: raise UnexpectedEndOfStream()
            key = key_stream.read()
        return key_type, key

    @classmethod
    def get_fullkey_from_keytype_and_key(cls, key_type: int, key: bytes) -> bytes:
        key_type_bytes = bytes.fromhex(var_int(key_type))
        return key_type_bytes + key

    def _serialize_psbt_section(self, fd):
        wr = self.create_psbt_writer(fd)
        self.serialize_psbt_section_kvs(wr)
        fd.write(b'\x00')  # section-separator

    def parse_psbt_section_kv(self, kt: int, key: bytes, val: bytes) -> None:
        raise NotImplementedError()  # implemented by subclasses

    def serialize_psbt_section_kvs(self, wr) -> None:
        raise NotImplementedError()  # implemented by subclasses


class PartialTxInput(TxInput, PSBTSection):
    def __init__(self, *args, **kwargs):
        TxInput.__init__(self, *args, **kwargs)
        self._utxo = None  # type: Optional[Transaction]
        self._witness_utxo = None  # type: Optional[TxOutput]
        self.part_sigs = {}  # type: Dict[bytes, bytes]  # pubkey -> sig
        self.bip32_paths = {}  # type: Dict[bytes, Tuple[bytes, Sequence[int]]]  # pubkey -> (xpub_fingerprint, path)
        self.redeem_script = None  # type: Optional[bytes]
        self.witness_script = None  # type: Optional[bytes]
        self._unknown = {}  # type: Dict[bytes, bytes]

        self.script_type = 'unknown'
        self.num_sig = 0  # type: int  # num req sigs for multisig
        self.pubkeys = []  # type: List[bytes]  # note: order matters
        self.__trusted_value_sats = None  # type: Optional[RavenValue]
        self._trusted_address = None  # type: Optional[str]
        self.block_height = None  # type: Optional[int]  # height at which the TXO is mined; None means unknown
        self.spent_height = None  # type: Optional[int]  # height at which the TXO got spent
        self.spent_txid = None  # type: Optional[str]  # txid of the spender
        self._is_p2sh_segwit = None  # type: Optional[bool]  # None means unknown
        self._is_native_segwit = None  # type: Optional[bool]  # None means unknown
        self.witness_sizehint = None  # type: Optional[int]  # byte size of serialized complete witness, for tx size est

    @property
    def _trusted_value_sats(self):
        return self.__trusted_value_sats

    @_trusted_value_sats.setter
    def _trusted_value_sats(self, v):
        assert isinstance(v, RavenValue)
        self.__trusted_value_sats = v

    @property
    def utxo(self):
        return self._utxo

    @utxo.setter
    def utxo(self, tx: Optional[Transaction]):
        if tx is None:
            return
        # note that tx might be a PartialTransaction
        # serialize and de-serialize tx now. this might e.g. convert a complete PartialTx to a Tx
        tx = tx_from_any(str(tx))
        # 'utxo' field in PSBT cannot be another PSBT:
        if not tx.is_complete():
            return
        self._utxo = tx
        self.validate_data()
        self.ensure_there_is_only_one_utxo()

    @property
    def witness_utxo(self):
        return self._witness_utxo

    @witness_utxo.setter
    def witness_utxo(self, value: Optional[TxOutput]):
        self._witness_utxo = value
        self.validate_data()
        self.ensure_there_is_only_one_utxo()

    def to_json(self):
        d = super().to_json()
        d.update({
            'height': self.block_height,
            'value_sats': self.value_sats(),
            'address': self.address,
            'utxo': str(self.utxo) if self.utxo else None,
            'witness_utxo': self.witness_utxo.serialize_to_network().hex() if self.witness_utxo else None,
            'sighash': self.sighash,
            'script_sig': self.script_sig,
            'redeem_script': self.redeem_script.hex() if self.redeem_script else None,
            'witness_script': self.witness_script.hex() if self.witness_script else None,
            'part_sigs': {pubkey.hex(): sig.hex() for pubkey, sig in self.part_sigs.items()},
            'bip32_paths': {pubkey.hex(): (xfp.hex(), bip32.convert_bip32_intpath_to_strpath(path))
                            for pubkey, (xfp, path) in self.bip32_paths.items()},
            'unknown_psbt_fields': {key.hex(): val.hex() for key, val in self._unknown.items()},
        })
        return d

    @classmethod
    def from_txin(cls, txin: TxInput, *, strip_witness: bool = True) -> 'PartialTxInput':
        # FIXME: if strip_witness is True, res.is_segwit() will return False,
        # and res.estimated_size() will return an incorrect value. These methods
        # will return the correct values after we call add_input_info(). (see dscancel and bump_fee)
        # This is very fragile: the value returned by estimate_size() depends on the calling order.
        res = PartialTxInput(prevout=txin.prevout,
                             script_sig=None if strip_witness else txin.script_sig,
                             nsequence=txin.nsequence,
                             witness=None if strip_witness else txin.witness,
                             is_coinbase_output=txin.is_coinbase_output(),
                             sighash=txin.sighash)
        return res

    def validate_data(self, *, for_signing=False) -> None:
        if self.utxo:
            if self.prevout.txid.hex() != self.utxo.txid():
                raise PSBTInputConsistencyFailure(f"PSBT input validation: "
                                                  f"If a non-witness UTXO is provided, its hash must match the hash specified in the prevout")
            if self.witness_utxo:
                if self.utxo.outputs()[self.prevout.out_idx] != self.witness_utxo:
                    raise PSBTInputConsistencyFailure(f"PSBT input validation: "
                                                      f"If both non-witness UTXO and witness UTXO are provided, they must be consistent")
        # The following test is disabled, so we are willing to sign non-segwit inputs
        # without verifying the input amount. This means, given a maliciously modified PSBT,
        # for non-segwit inputs, we might end up burning coins as miner fees.
        if for_signing and False:
            if not self.is_segwit() and self.witness_utxo:
                raise PSBTInputConsistencyFailure(f"PSBT input validation: "
                                                  f"If a witness UTXO is provided, no non-witness signature may be created")
        if self.redeem_script and self.address:
            addr = hash160_to_p2sh(hash_160(self.redeem_script))
            if self.address != addr:
                raise PSBTInputConsistencyFailure(f"PSBT input validation: "
                                                  f"If a redeemScript is provided, the scriptPubKey must be for that redeemScript")
        if self.witness_script:
            if self.redeem_script:
                if self.redeem_script != bfh(ravencoin.p2wsh_nested_script(self.witness_script.hex())):
                    raise PSBTInputConsistencyFailure(f"PSBT input validation: "
                                                      f"If a witnessScript is provided, the redeemScript must be for that witnessScript")
            elif self.address:
                if self.address != ravencoin.script_to_p2wsh(self.witness_script.hex()):
                    raise PSBTInputConsistencyFailure(f"PSBT input validation: "
                                                      f"If a witnessScript is provided, the scriptPubKey must be for that witnessScript")

    def parse_psbt_section_kv(self, kt, key, val):
        try:
            kt = PSBTInputType(kt)
        except ValueError:
            pass  # unknown type
        if DEBUG_PSBT_PARSING: print(f"{repr(kt)} {key.hex()} {val.hex()}")
        if kt == PSBTInputType.NON_WITNESS_UTXO:
            if self.utxo is not None:
                raise SerializationError(f"duplicate key: {repr(kt)}")
            self.utxo = Transaction(val)
            self.utxo.deserialize()
            if key: raise SerializationError(f"key for {repr(kt)} must be empty")
        elif kt == PSBTInputType.WITNESS_UTXO:
            if self.witness_utxo is not None:
                raise SerializationError(f"duplicate key: {repr(kt)}")
            self.witness_utxo = TxOutput.from_network_bytes(val)
            if key: raise SerializationError(f"key for {repr(kt)} must be empty")
        elif kt == PSBTInputType.PARTIAL_SIG:
            if key in self.part_sigs:
                raise SerializationError(f"duplicate key: {repr(kt)}")
            if len(key) not in (33, 65):  # TODO also allow 32? one of the tests in the BIP is "supposed to" fail with len==32...
                raise SerializationError(f"key for {repr(kt)} has unexpected length: {len(key)}")
            self.part_sigs[key] = val
        elif kt == PSBTInputType.SIGHASH_TYPE:
            if self.sighash is not None:
                raise SerializationError(f"duplicate key: {repr(kt)}")
            if len(val) != 4:
                raise SerializationError(f"value for {repr(kt)} has unexpected length: {len(val)}")
            self.sighash = struct.unpack("<I", val)[0]
            if key: raise SerializationError(f"key for {repr(kt)} must be empty")
        elif kt == PSBTInputType.BIP32_DERIVATION:
            if key in self.bip32_paths:
                raise SerializationError(f"duplicate key: {repr(kt)}")
            if len(key) not in (33, 65):  # TODO also allow 32? one of the tests in the BIP is "supposed to" fail with len==32...
                raise SerializationError(f"key for {repr(kt)} has unexpected length: {len(key)}")
            self.bip32_paths[key] = unpack_bip32_root_fingerprint_and_int_path(val)
        elif kt == PSBTInputType.REDEEM_SCRIPT:
            if self.redeem_script is not None:
                raise SerializationError(f"duplicate key: {repr(kt)}")
            self.redeem_script = val
            if key: raise SerializationError(f"key for {repr(kt)} must be empty")
        elif kt == PSBTInputType.WITNESS_SCRIPT:
            if self.witness_script is not None:
                raise SerializationError(f"duplicate key: {repr(kt)}")
            self.witness_script = val
            if key: raise SerializationError(f"key for {repr(kt)} must be empty")
        elif kt == PSBTInputType.FINAL_SCRIPTSIG:
            if self.script_sig is not None:
                raise SerializationError(f"duplicate key: {repr(kt)}")
            self.script_sig = val
            if key: raise SerializationError(f"key for {repr(kt)} must be empty")
        elif kt == PSBTInputType.FINAL_SCRIPTWITNESS:
            if self.witness is not None:
                raise SerializationError(f"duplicate key: {repr(kt)}")
            self.witness = val
            if key: raise SerializationError(f"key for {repr(kt)} must be empty")
        else:
            full_key = self.get_fullkey_from_keytype_and_key(kt, key)
            if full_key in self._unknown:
                raise SerializationError(f'duplicate key. PSBT input key for unknown type: {full_key}')
            self._unknown[full_key] = val

    def serialize_psbt_section_kvs(self, wr):
        self.ensure_there_is_only_one_utxo()
        if self.witness_utxo:
            wr(PSBTInputType.WITNESS_UTXO, self.witness_utxo.serialize_to_network())
        if self.utxo:
            wr(PSBTInputType.NON_WITNESS_UTXO, bfh(self.utxo.serialize_to_network(include_sigs=True)))
        for pk, val in sorted(self.part_sigs.items()):
            wr(PSBTInputType.PARTIAL_SIG, val, pk)
        if self.sighash is not None:
            wr(PSBTInputType.SIGHASH_TYPE, struct.pack('<I', self.sighash))
        if self.redeem_script is not None:
            wr(PSBTInputType.REDEEM_SCRIPT, self.redeem_script)
        if self.witness_script is not None:
            wr(PSBTInputType.WITNESS_SCRIPT, self.witness_script)
        for k in sorted(self.bip32_paths):
            packed_path = pack_bip32_root_fingerprint_and_int_path(*self.bip32_paths[k])
            wr(PSBTInputType.BIP32_DERIVATION, packed_path, k)
        if self.script_sig is not None:
            wr(PSBTInputType.FINAL_SCRIPTSIG, self.script_sig)
        if self.witness is not None:
            wr(PSBTInputType.FINAL_SCRIPTWITNESS, self.witness)
        for full_key, val in sorted(self._unknown.items()):
            key_type, key = self.get_keytype_and_key_from_fullkey(full_key)
            wr(key_type, val, key=key)

    def value_sats(self) -> Optional[RavenValue]:
        if self._trusted_value_sats is not None:
            return self._trusted_value_sats
        if self.utxo:
            out_idx = self.prevout.out_idx
            outpoint = self.utxo.outputs()[out_idx]
            if outpoint.asset:
                value = RavenValue(0, {outpoint.asset: outpoint.value})
            else:
                value = RavenValue(outpoint.value)
            return value
        if self.witness_utxo:
            outpoint = self.witness_utxo
            if outpoint.asset:
                value = RavenValue(0, {outpoint.asset: outpoint.value})
            else:
                value = RavenValue(outpoint.value)
            return value
        return None

    @property
    def address(self) -> Optional[str]:
        if self._trusted_address is not None:
            return self._trusted_address
        scriptpubkey = self.scriptpubkey
        if scriptpubkey:
            return get_address_from_output_script(scriptpubkey)
        return None

    @property
    def scriptpubkey(self) -> Optional[bytes]:
        if self._trusted_address is not None:
            a = self.value_sats().assets
            script = bfh(ravencoin.address_to_script(self._trusted_address))
            #if a:
            #    asset, amt = list(a.items())[0]
            #    script = assets.create_transfer_asset_script(script, asset, amt)
            return script
        if self.utxo:
            out_idx = self.prevout.out_idx
            return self.utxo.outputs()[out_idx].scriptpubkey
        if self.witness_utxo:
            return self.witness_utxo.scriptpubkey
        return None

    def set_script_type(self) -> None:
        if self.scriptpubkey is None:
            return
        type = get_script_type_from_output_script(self.scriptpubkey)
        inner_type = None
        if type is not None:
            if type == 'p2sh':
                inner_type = get_script_type_from_output_script(self.redeem_script)
            elif type == 'p2wsh':
                inner_type = get_script_type_from_output_script(self.witness_script)
            if inner_type is not None:
                type = inner_type + '-' + type
            if type in ('p2pkh', 'p2wpkh-p2sh', 'p2wpkh', 'p2pk'):
                self.script_type = type
        return

    def is_complete(self) -> bool:
        if self.script_sig is not None and self.witness is not None:
            return True
        if self.is_coinbase_input():
            return True
        if self.script_sig is not None and not self.is_segwit():
            return True
        signatures = list(self.part_sigs.values())
        s = len(signatures)
        # note: The 'script_type' field is currently only set by the wallet,
        #       for its own addresses. This means we can only finalize inputs
        #       that are related to the wallet.
        #       The 'fix' would be adding extra logic that matches on templates,
        #       and figures out the script_type from available fields.
        if self.script_type in ('p2pk', 'p2pkh', 'p2wpkh', 'p2wpkh-p2sh'):
            return s >= 1
        if self.script_type in ('p2sh', 'p2wsh', 'p2wsh-p2sh'):
            return s >= self.num_sig
        return False

    def finalize(self) -> None:
        def clear_fields_when_finalized():
            # BIP-174: "All other data except the UTXO and unknown fields in the
            #           input key-value map should be cleared from the PSBT"
            self.part_sigs = {}
            #self.sighash = None
            self.bip32_paths = {}
            self.redeem_script = None
            self.witness_script = None

        if self.script_sig is not None and self.witness is not None:
            clear_fields_when_finalized()
            return  # already finalized
        if self.is_complete():
            self.script_sig = bfh(Transaction.input_script(self))
            self.witness = bfh(Transaction.serialize_witness(self))
            clear_fields_when_finalized()

    def combine_with_other_txin(self, other_txin: 'TxInput') -> None:
        assert self.prevout == other_txin.prevout
        if other_txin.script_sig is not None:
            self.script_sig = other_txin.script_sig
        if other_txin.witness is not None:
            self.witness = other_txin.witness
        if isinstance(other_txin, PartialTxInput):
            if other_txin.witness_utxo:
                self.witness_utxo = other_txin.witness_utxo
            if other_txin.utxo:
                self.utxo = other_txin.utxo
            self.part_sigs.update(other_txin.part_sigs)
            if other_txin.sighash is not None:
                self.sighash = other_txin.sighash
            self.bip32_paths.update(other_txin.bip32_paths)
            if other_txin.redeem_script is not None:
                self.redeem_script = other_txin.redeem_script
            if other_txin.witness_script is not None:
                self.witness_script = other_txin.witness_script
            self._unknown.update(other_txin._unknown)
        self.ensure_there_is_only_one_utxo()
        # try to finalize now
        self.finalize()

    def ensure_there_is_only_one_utxo(self):
        # we prefer having the full previous tx, even for segwit inputs. see #6198
        # for witness v1, witness_utxo will be enough though
        if self.utxo is not None and self.witness_utxo is not None:
            self.witness_utxo = None

    def convert_utxo_to_witness_utxo(self) -> None:
        if self.utxo:
            self._witness_utxo = self.utxo.outputs()[self.prevout.out_idx]
            self._utxo = None  # type: Optional[Transaction]

    def is_native_segwit(self) -> Optional[bool]:
        """Whether this input is native segwit. None means inconclusive."""
        if self._is_native_segwit is None:
            if self.address:
                self._is_native_segwit = ravencoin.is_segwit_address(self.address)
        return self._is_native_segwit

    def is_p2sh_segwit(self) -> Optional[bool]:
        """Whether this input is p2sh-embedded-segwit. None means inconclusive."""
        if self._is_p2sh_segwit is None:
            def calc_if_p2sh_segwit_now():
                if not (self.address and self.redeem_script):
                    return None
                if self.address != ravencoin.hash160_to_p2sh(hash_160(self.redeem_script)):
                    # not p2sh address
                    return False
                try:
                    decoded = [x for x in script_GetOp(self.redeem_script)]
                except MalformedBitcoinScript:
                    decoded = None
                # witness version 0
                if match_script_against_template(decoded, SCRIPTPUBKEY_TEMPLATE_WITNESS_V0):
                    return True
                # witness version 1-16
                future_witness_versions = list(range(opcodes.OP_1, opcodes.OP_16 + 1))
                for witver, opcode in enumerate(future_witness_versions, start=1):
                    match = [opcode, OPPushDataGeneric(lambda x: 2 <= x <= 40)]
                    if match_script_against_template(decoded, match):
                        return True
                return False

            self._is_p2sh_segwit = calc_if_p2sh_segwit_now()
        return self._is_p2sh_segwit

    def is_segwit(self, *, guess_for_address=False) -> bool:
        if super().is_segwit():
            return True
        if self.is_native_segwit() or self.is_p2sh_segwit():
            return True
        if self.is_native_segwit() is False and self.is_p2sh_segwit() is False:
            return False
        if self.witness_script:
            return True
        _type = self.script_type
        if _type == 'address' and guess_for_address:
            _type = Transaction.guess_txintype_from_address(self.address)
        return is_segwit_script_type(_type)

    def already_has_some_signatures(self) -> bool:
        """Returns whether progress has been made towards completing this input."""
        return (self.part_sigs
                or self.script_sig is not None
                or self.witness is not None)


class PartialTxOutput(TxOutput, PSBTSection):
    def __init__(self, *args, **kwargs):
        TxOutput.__init__(self, *args, **kwargs)
        self.redeem_script = None  # type: Optional[bytes]
        self.witness_script = None  # type: Optional[bytes]
        self.bip32_paths = {}  # type: Dict[bytes, Tuple[bytes, Sequence[int]]]  # pubkey -> (xpub_fingerprint, path)
        self._unknown = {}  # type: Dict[bytes, bytes]

        self.script_type = 'unknown'
        self.num_sig = 0  # num req sigs for multisig
        self.pubkeys = []  # type: List[bytes]  # note: order matters
        self.is_mine = False  # type: bool  # whether the wallet considers the output to be ismine
        self.is_change = False  # type: bool  # whether the wallet considers the output to be change

    def to_json(self):
        d = super().to_json()
        d.update({
            'redeem_script': self.redeem_script.hex() if self.redeem_script else None,
            'witness_script': self.witness_script.hex() if self.witness_script else None,
            'bip32_paths': {pubkey.hex(): (xfp.hex(), bip32.convert_bip32_intpath_to_strpath(path))
                            for pubkey, (xfp, path) in self.bip32_paths.items()},
            'unknown_psbt_fields': {key.hex(): val.hex() for key, val in self._unknown.items()},
        })
        return d

    @classmethod
    def from_txout(cls, txout: TxOutput) -> 'PartialTxOutput':
        res = PartialTxOutput(scriptpubkey=txout.scriptpubkey,
                              value=txout.value,
                              asset=txout.asset)
        return res

    def parse_psbt_section_kv(self, kt, key, val):
        try:
            kt = PSBTOutputType(kt)
        except ValueError:
            pass  # unknown type
        if DEBUG_PSBT_PARSING: print(f"{repr(kt)} {key.hex()} {val.hex()}")
        if kt == PSBTOutputType.REDEEM_SCRIPT:
            if self.redeem_script is not None:
                raise SerializationError(f"duplicate key: {repr(kt)}")
            self.redeem_script = val
            if key: raise SerializationError(f"key for {repr(kt)} must be empty")
        elif kt == PSBTOutputType.WITNESS_SCRIPT:
            if self.witness_script is not None:
                raise SerializationError(f"duplicate key: {repr(kt)}")
            self.witness_script = val
            if key: raise SerializationError(f"key for {repr(kt)} must be empty")
        elif kt == PSBTOutputType.BIP32_DERIVATION:
            if key in self.bip32_paths:
                raise SerializationError(f"duplicate key: {repr(kt)}")
            if len(key) not in (33, 65):  # TODO also allow 32? one of the tests in the BIP is "supposed to" fail with len==32...
                raise SerializationError(f"key for {repr(kt)} has unexpected length: {len(key)}")
            self.bip32_paths[key] = unpack_bip32_root_fingerprint_and_int_path(val)
        else:
            full_key = self.get_fullkey_from_keytype_and_key(kt, key)
            if full_key in self._unknown:
                raise SerializationError(f'duplicate key. PSBT output key for unknown type: {full_key}')
            self._unknown[full_key] = val

    def serialize_psbt_section_kvs(self, wr):
        if self.redeem_script is not None:
            wr(PSBTOutputType.REDEEM_SCRIPT, self.redeem_script)
        if self.witness_script is not None:
            wr(PSBTOutputType.WITNESS_SCRIPT, self.witness_script)
        for k in sorted(self.bip32_paths):
            packed_path = pack_bip32_root_fingerprint_and_int_path(*self.bip32_paths[k])
            wr(PSBTOutputType.BIP32_DERIVATION, packed_path, k)
        for full_key, val in sorted(self._unknown.items()):
            key_type, key = self.get_keytype_and_key_from_fullkey(full_key)
            wr(key_type, val, key=key)

    def combine_with_other_txout(self, other_txout: 'TxOutput') -> None:
        assert self.scriptpubkey == other_txout.scriptpubkey
        if not isinstance(other_txout, PartialTxOutput):
            return
        if other_txout.redeem_script is not None:
            self.redeem_script = other_txout.redeem_script
        if other_txout.witness_script is not None:
            self.witness_script = other_txout.witness_script
        self.bip32_paths.update(other_txout.bip32_paths)
        self._unknown.update(other_txout._unknown)


class PartialTransaction(Transaction):

    def __init__(self):
        Transaction.__init__(self, None)
        self.xpubs = {}  # type: Dict[BIP32Node, Tuple[bytes, Sequence[int]]]  # intermediate bip32node -> (xfp, der_prefix)
        self._inputs = []  # type: List[PartialTxInput]
        self._outputs = []  # type: List[PartialTxOutput]
        self._unknown = {}  # type: Dict[bytes, bytes]
        self._prevout_overrides = {}  # type: Dict[str, bytes]

    def to_json(self) -> dict:
        d = super().to_json()
        d.update({
            'xpubs': {bip32node.to_xpub(): (xfp.hex(), bip32.convert_bip32_intpath_to_strpath(path))
                      for bip32node, (xfp, path) in self.xpubs.items()},
            'unknown_psbt_fields': {key.hex(): val.hex() for key, val in self._unknown.items()},
        })
        return d

    @classmethod
    def from_tx(cls, tx: Transaction, strip = True) -> 'PartialTransaction':
        res = cls()
        res._inputs = [PartialTxInput.from_txin(txin, strip_witness=strip)
                       for txin in tx.inputs()]
        res._outputs = [PartialTxOutput.from_txout(txout) for txout in tx.outputs()]
        res.version = tx.version
        res.locktime = tx.locktime
        res.for_swap = tx.for_swap
        return res

    @classmethod
    def from_raw_psbt(cls, raw) -> 'PartialTransaction':
        # auto-detect and decode Base64 and Hex.
        if raw[0:10].lower() in (b'70736274ff', '70736274ff'):  # hex
            raw = bytes.fromhex(raw)
        elif raw[0:6] in (b'cHNidP', 'cHNidP'):  # base64
            raw = base64.b64decode(raw)
        if not isinstance(raw, (bytes, bytearray)) or raw[0:5] != b'psbt\xff':
            raise BadHeaderMagic("bad magic")

        tx = None  # type: Optional[PartialTransaction]

        # We parse the raw stream twice. The first pass is used to find the
        # PSBT_GLOBAL_UNSIGNED_TX key in the global section and set 'tx'.
        # The second pass does everything else.
        with io.BytesIO(raw[5:]) as fd:  # parsing "first pass"
            while True:
                try:
                    kt, key, val = PSBTSection.get_next_kv_from_fd(fd)
                except StopIteration:
                    break
                try:
                    kt = PSBTGlobalType(kt)
                except ValueError:
                    pass  # unknown type
                if kt == PSBTGlobalType.UNSIGNED_TX:
                    if tx is not None:
                        raise SerializationError(f"duplicate key: {repr(kt)}")
                    if key: raise SerializationError(f"key for {repr(kt)} must be empty")
                    unsigned_tx = Transaction(val.hex())
                    for txin in unsigned_tx.inputs():
                        if txin.script_sig or txin.witness:
                            raise SerializationError(f"PSBT {repr(kt)} must have empty scriptSigs and witnesses")
                    tx = PartialTransaction.from_tx(unsigned_tx)

        if tx is None:
            raise SerializationError(f"PSBT missing required global section PSBT_GLOBAL_UNSIGNED_TX")

        with io.BytesIO(raw[5:]) as fd:  # parsing "second pass"
            # global section
            while True:
                try:
                    kt, key, val = PSBTSection.get_next_kv_from_fd(fd)
                except StopIteration:
                    break
                try:
                    kt = PSBTGlobalType(kt)
                except ValueError:
                    pass  # unknown type
                if DEBUG_PSBT_PARSING: print(f"{repr(kt)} {key.hex()} {val.hex()}")
                if kt == PSBTGlobalType.UNSIGNED_TX:
                    pass  # already handled during "first" parsing pass
                elif kt == PSBTGlobalType.XPUB:
                    bip32node = BIP32Node.from_bytes(key)
                    if bip32node in tx.xpubs:
                        raise SerializationError(f"duplicate key: {repr(kt)}")
                    xfp, path = unpack_bip32_root_fingerprint_and_int_path(val)
                    if bip32node.depth != len(path):
                        raise SerializationError(f"PSBT global xpub has mismatching depth ({bip32node.depth}) "
                                                 f"and derivation prefix len ({len(path)})")
                    child_number_of_xpub = int.from_bytes(bip32node.child_number, 'big')
                    if not ((bip32node.depth == 0 and child_number_of_xpub == 0)
                            or (bip32node.depth != 0 and child_number_of_xpub == path[-1])):
                        raise SerializationError(f"PSBT global xpub has inconsistent child_number and derivation prefix")
                    tx.xpubs[bip32node] = xfp, path
                elif kt == PSBTGlobalType.VERSION:
                    if len(val) > 4:
                        raise SerializationError(f"value for {repr(kt)} has unexpected length: {len(val)} > 4")
                    psbt_version = int.from_bytes(val, byteorder='little', signed=False)
                    if psbt_version > 0:
                        raise SerializationError(f"Only PSBTs with version 0 are supported. Found version: {psbt_version}")
                    if key: raise SerializationError(f"key for {repr(kt)} must be empty")
                else:
                    full_key = PSBTSection.get_fullkey_from_keytype_and_key(kt, key)
                    if full_key in tx._unknown:
                        raise SerializationError(f'duplicate key. PSBT global key for unknown type: {full_key}')
                    tx._unknown[full_key] = val
            try:
                # inputs sections
                for txin in tx.inputs():
                    if DEBUG_PSBT_PARSING: print("-> new input starts")
                    txin._populate_psbt_fields_from_fd(fd)
                # outputs sections
                for txout in tx.outputs():
                    if DEBUG_PSBT_PARSING: print("-> new output starts")
                    txout._populate_psbt_fields_from_fd(fd)
            except UnexpectedEndOfStream:
                raise UnexpectedEndOfStream('Unexpected end of stream. Num input and output maps provided does not match unsigned tx.') from None

            if fd.read(1) != b'':
                raise SerializationError("extra junk at the end of PSBT")

        for txin in tx.inputs():
            txin.validate_data()

        return tx

    @classmethod
    def from_io(cls, inputs: Sequence[PartialTxInput], outputs: Sequence[PartialTxOutput], *, wallet = None,
                locktime: int = None, version: int = None, BIP69_sort: bool = True, for_swap = False):
        self = cls()
        self._inputs = list(inputs)
        self._outputs = list(outputs)
        self._wallet = wallet
        if locktime is not None:
            self.locktime = locktime
        if version is not None:
            self.version = version
        self.for_swap = for_swap
        if BIP69_sort:
            self.BIP69_sort()
        return self

    def _serialize_psbt(self, fd) -> None:
        wr = PSBTSection.create_psbt_writer(fd)
        fd.write(b'psbt\xff')
        # global section
        wr(PSBTGlobalType.UNSIGNED_TX, bfh(self.serialize_to_network(include_sigs=False)))
        for bip32node, (xfp, path) in sorted(self.xpubs.items()):
            val = pack_bip32_root_fingerprint_and_int_path(xfp, path)
            wr(PSBTGlobalType.XPUB, val, key=bip32node.to_bytes())
        for full_key, val in sorted(self._unknown.items()):
            key_type, key = PSBTSection.get_keytype_and_key_from_fullkey(full_key)
            wr(key_type, val, key=key)
        fd.write(b'\x00')  # section-separator
        # input sections
        for inp in self._inputs:
            inp._serialize_psbt_section(fd)
        # output sections
        for outp in self._outputs:
            outp._serialize_psbt_section(fd)

    def finalize_psbt(self) -> None:
        for txin in self.inputs():
            txin.finalize()

    def combine_with_other_psbt(self, other_tx: 'Transaction') -> None:
        """Pulls in all data from other_tx we don't yet have (e.g. signatures).
        other_tx must be concerning the same unsigned tx.
        """
        if self.serialize_to_network(include_sigs=False) != other_tx.serialize_to_network(include_sigs=False):
            raise Exception('A Combiner must not combine two different PSBTs.')
        # BIP-174: "The resulting PSBT must contain all of the key-value pairs from each of the PSBTs.
        #           The Combiner must remove any duplicate key-value pairs, in accordance with the specification."
        # global section
        if isinstance(other_tx, PartialTransaction):
            self.xpubs.update(other_tx.xpubs)
            self._unknown.update(other_tx._unknown)
        # input sections
        for txin, other_txin in zip(self.inputs(), other_tx.inputs()):
            txin.combine_with_other_txin(other_txin)
        # output sections
        for txout, other_txout in zip(self.outputs(), other_tx.outputs()):
            txout.combine_with_other_txout(other_txout)
        self.invalidate_ser_cache()

    def join_with_other_psbt(self, other_tx: 'PartialTransaction') -> None:
        """Adds inputs and outputs from other_tx into this one."""
        if not isinstance(other_tx, PartialTransaction):
            raise Exception('Can only join partial transactions.')
        # make sure there are no duplicate prevouts
        prevouts = set()
        for txin in itertools.chain(self.inputs(), other_tx.inputs()):
            prevout_str = txin.prevout.to_str()
            if prevout_str in prevouts:
                raise Exception(f"Duplicate inputs! "
                                f"Transactions that spend the same prevout cannot be joined.")
            prevouts.add(prevout_str)
        # copy global PSBT section
        self.xpubs.update(other_tx.xpubs)
        self._unknown.update(other_tx._unknown)
        # copy and add inputs and outputs
        self.add_inputs(list(other_tx.inputs()))
        self.add_outputs(list(other_tx.outputs()))
        self.remove_signatures()
        self.invalidate_ser_cache()

    def inputs(self) -> Sequence[PartialTxInput]:
        return self._inputs

    def outputs(self) -> Sequence[PartialTxOutput]:
        return self._outputs

    def add_inputs(self, inputs: List[PartialTxInput]) -> None:
        self._inputs.extend(inputs)
        self.BIP69_sort(outputs=False)
        self.invalidate_ser_cache()

    def add_outputs(self, outputs: List[PartialTxOutput]) -> None:
        self._outputs.extend(outputs)
        self.BIP69_sort(inputs=False)
        self.invalidate_ser_cache()

    def set_rbf(self, rbf: bool) -> None:
        nSequence = 0xffffffff - (2 if rbf else 1)
        for txin in self.inputs():
            # Ensure 0 for SIGHASH_SINGLE
            if self.for_swap and txin.sighash and txin.sighash & SIGHASH.SINGLE != 0:
                continue
            txin.nsequence = nSequence
        self.invalidate_ser_cache()

    def BIP69_sort(self, inputs=True, outputs=True):
        # Do not change the ordering for SIGHASH_SINGLE
        if self.for_swap:
            return
        # NOTE: other parts of the code rely on these sorts being *stable* sorts
        if inputs:
            self._inputs.sort(key = lambda i: (i.prevout.txid, i.prevout.out_idx))
        if outputs:
            self._outputs.sort(key = lambda o: (o.value, o.scriptpubkey))

            # Assets need a certain order:
            # Burn, {whatever}, parent, new owner, new
            burn_vout = None
            parent_owner_vout = None
            asset_owner_vout = None
            asset_create_vout = None
            for o in iter(self._outputs):
                if o.address in constants.net.BURN_ADDRESSES:
                    burn_vout = o
                elif o.asset:
                    asset = o.asset
                    if asset[-1] == '!':
                        if asset_create_vout:
                            # We know what the asset owner looks like
                            if asset[:-1] == asset_create_vout.asset:
                                t = asset_owner_vout
                                asset_owner_vout = o
                                if not parent_owner_vout:
                                    parent_owner_vout = t
                            else:
                                parent_owner_vout = o
                        else:
                            # Just put it somewhere
                            if asset_owner_vout:
                                parent_owner_vout = o
                            else:
                                asset_owner_vout = o
                    else:
                        asset_create_vout = o
                        if asset_owner_vout and asset_owner_vout.asset[:-1] != asset:
                            # Swap asset owner and parent owner positions
                            t = parent_owner_vout
                            parent_owner_vout = asset_owner_vout
                            asset_owner_vout = t
            if burn_vout and asset_create_vout:
                new_outs = [o for o in self._outputs if o not in (burn_vout, parent_owner_vout, asset_owner_vout, asset_create_vout)]

                self._outputs = [burn_vout] + new_outs + \
                                ([parent_owner_vout] if parent_owner_vout else []) + \
                                ([asset_owner_vout] if asset_owner_vout else []) + \
                                [asset_create_vout]
        self.invalidate_ser_cache()

    def input_value(self) -> RavenValue:
        input_values = [txin.value_sats() for txin in self.inputs()]
        if any([val is None for val in input_values]):
            raise MissingTxInputAmount()
        return sum(input_values, RavenValue())

    def output_value(self) -> RavenValue:
        return \
            sum([RavenValue(0, {x.asset: x.value}) if x.asset else RavenValue(x.value) for x in self.outputs()], RavenValue())

    def get_fee(self) -> Optional[RavenValue]:
        try:
            return self.input_value() - self.output_value()
        except MissingTxInputAmount:
            return None

    def serialize_preimage(self, txin_index: int, *,
                           bip143_shared_txdigest_fields: BIP143SharedTxDigestFields = None) -> str:
        nVersion = int_to_hex(self.version, 4)
        nLocktime = int_to_hex(self.locktime, 4)
        inputs = self.inputs()
        outputs = self.outputs()
        txin = inputs[txin_index]
        sighash = txin.sighash if txin.sighash is not None else SIGHASH.ALL
        if sighash not in list(map(int, SIGHASH)):
            raise Exception("invalid sighash: {}".format(sighash))
        nHashType = int_to_hex(sighash, 4)
        preimage_script = self.get_preimage_script(txin, self._wallet, txin_locking_script_overrides=self._prevout_overrides)
        _logger.info(f"Preimage script for {txin.prevout.txid.hex()}\n{bfh(preimage_script)}")
        if txin.is_segwit():
            raise NotImplementedError()
            if bip143_shared_txdigest_fields is None:
                bip143_shared_txdigest_fields = self._calc_bip143_shared_txdigest_fields()
            if not(sighash & SIGHASH.ANYONECANPAY):
                hashPrevouts = bip143_shared_txdigest_fields.hashPrevouts
            else:
                hashPrevouts = '00' * 32
            if (not(sighash & SIGHASH.ANYONECANPAY) and (sighash & 0x1f) != SIGHASH.SINGLE and (sighash & 0x1f) != SIGHASH.NONE):
                hashSequence = bip143_shared_txdigest_fields.hashSequence
            else:
                hashSequence = '00' * 32
            if ((sighash & 0x1f) != SIGHASH.SINGLE and (sighash & 0x1f) != SIGHASH.NONE):
                hashOutputs = bip143_shared_txdigest_fields.hashOutputs
            elif ((sighash & 0x1f) == SIGHASH.SINGLE and txin_index < len(outputs)):
                hashOutputs = bh2u(sha256d(outputs[txin_index].serialize_to_network()))
            else:
                hashOutputs = '00' * 32
            outpoint = txin.prevout.serialize_to_network().hex()
            scriptCode = var_int(len(preimage_script) // 2) + preimage_script
            amount = int_to_hex(txin.value_sats(), 8)
            nSequence = int_to_hex(txin.nsequence, 4)
            preimage = nVersion + hashPrevouts + hashSequence + outpoint + scriptCode + amount + nSequence + hashOutputs + nLocktime + nHashType
        else:
            if sighash & int(SIGHASH.ANYONECANPAY) != 0:
                txins = var_int(1) + self.serialize_input(txin, preimage_script)
                # We only need to check the next 2 bits now
                if sighash & int(SIGHASH.SINGLE) == 0:
                    raise NotImplementedError()
            else:
                txins = var_int(len(inputs))
                for k, txin in enumerate(inputs):
                    if (sighash == int(SIGHASH.NONE) or sighash == int(SIGHASH.SINGLE)) and txin_index != k:
                        txin = PartialTxInput.from_txin(txin)
                        txin.nsequence = 0
                    s_in = self.serialize_input(txin, preimage_script if txin_index==k else '')
                    txins += s_in
            
            if sighash == int(SIGHASH.NONE):
                txouts = var_int(0)
            elif sighash == int(SIGHASH.SINGLE):
                if txin_index > len(outputs):
                    raise Exception("Not enough outputs for SIGHASH_SINGLE!")
                txouts = var_int(txin_index)
                for k, txout in enumerate(outputs):
                    if k < txin_index:
                        txout = PartialTxOutput.from_txout(txout)
                        txout.scriptpubkey = b''
                        txout.value = Satoshis((1 << 64) - 1)
                        txout.asset = None
                        txouts += txout.serialize_to_network().hex()
                    elif k == txin_index:
                        txouts += txout.serialize_to_network().hex()
                    else:
                        break
            else:
                txouts = var_int(len(outputs)) + ''.join(o.serialize_to_network().hex() for o in outputs)

            preimage = nVersion + txins + txouts + nLocktime + nHashType
        return preimage

    def sign(self, keypairs) -> None:
        # keypairs:  pubkey_hex -> (secret_bytes, is_compressed)
        bip143_shared_txdigest_fields = self._calc_bip143_shared_txdigest_fields()
        for i, txin in enumerate(self.inputs()):
            pubkeys = [pk.hex() for pk in txin.pubkeys]
            for pubkey in pubkeys:
                if txin.is_complete():
                    break
                if pubkey not in keypairs:
                    continue
                _logger.info(f"adding signature for {pubkey}")
                sec, compressed = keypairs[pubkey]
                sig = self.sign_txin(i, sec, bip143_shared_txdigest_fields=bip143_shared_txdigest_fields)
                self.add_signature_to_txin(txin_idx=i, signing_pubkey=pubkey, sig=sig)

        _logger.debug(f"is_complete {self.is_complete()}")
        self.invalidate_ser_cache()

    def sign_txin(self, txin_index, privkey_bytes, *, bip143_shared_txdigest_fields=None) -> str:
        txin = self.inputs()[txin_index]
        txin.validate_data(for_signing=True)
        sighash = txin.sighash if txin.sighash is not None else SIGHASH.ALL
        sighash_type = sighash.to_bytes(length=1, byteorder="big").hex()
        pre_hash = sha256d(bfh(self.serialize_preimage(txin_index,
                                                       bip143_shared_txdigest_fields=bip143_shared_txdigest_fields)))
        privkey = ecc.ECPrivkey(privkey_bytes)
        sig = privkey.sign_transaction(pre_hash)
        sig = bh2u(sig) + '{0:02x}'.format(txin.sighash if txin.sighash else SIGHASH.ALL)
        return sig

    def is_complete(self) -> bool:
        return all([txin.is_complete() for txin in self.inputs()])

    def signature_count(self) -> Tuple[int, int]:
        s = 0  # "num Sigs we have"
        r = 0  # "Required"
        for txin in self.inputs():
            if txin.is_coinbase_input():
                continue
            signatures = list(txin.part_sigs.values())
            s += len(signatures)
            r += txin.num_sig
        return s, r

    def serialize(self) -> str:
        """Returns PSBT as base64 text, or raw hex of network tx (if complete)."""
        self.finalize_psbt()
        if self.is_complete():
            return Transaction.serialize(self)
        return self._serialize_as_base64()

    def serialize_as_bytes(self, *, force_psbt: bool = False) -> bytes:
        """Returns PSBT as raw bytes, or raw bytes of network tx (if complete)."""
        self.finalize_psbt()
        if force_psbt or not self.is_complete():
            with io.BytesIO() as fd:
                self._serialize_psbt(fd)
                return fd.getvalue()
        else:
            return Transaction.serialize_as_bytes(self)

    def _serialize_as_base64(self) -> str:
        raw_bytes = self.serialize_as_bytes()
        return base64.b64encode(raw_bytes).decode('ascii')

    def update_signatures(self, signatures: Sequence[str]):
        """Add new signatures to a transaction

        `signatures` is expected to be a list of sigs with signatures[i]
        intended for self._inputs[i].
        This is used by the Trezor, KeepKey and Safe-T plugins.
        """
        if self.is_complete():
            return
        if len(self.inputs()) != len(signatures):
            raise Exception('expected {} signatures; got {}'.format(len(self.inputs()), len(signatures)))
        for i, txin in enumerate(self.inputs()):
            pubkeys = [pk.hex() for pk in txin.pubkeys]
            sig = signatures[i]
            if bfh(sig) in list(txin.part_sigs.values()):
                continue
            pre_hash = sha256d(bfh(self.serialize_preimage(i)))
            sig_string = ecc.sig_string_from_der_sig(bfh(sig[:-2]))
            for recid in range(4):
                try:
                    public_key = ecc.ECPubkey.from_sig_string(sig_string, recid, pre_hash)
                except ecc.InvalidECPointException:
                    # the point might not be on the curve for some recid values
                    continue
                pubkey_hex = public_key.get_public_key_hex(compressed=True)
                if pubkey_hex in pubkeys:
                    if not public_key.verify_message_hash(sig_string, pre_hash):
                        continue
                    _logger.info(f"adding sig: txin_idx={i}, signing_pubkey={pubkey_hex}, sig={sig}")
                    self.add_signature_to_txin(txin_idx=i, signing_pubkey=pubkey_hex, sig=sig)
                    break
        # redo raw
        self.invalidate_ser_cache()

    def add_signature_to_txin(self, *, txin_idx: int, signing_pubkey: str, sig: str):
        txin = self._inputs[txin_idx]
        txin.part_sigs[bfh(signing_pubkey)] = bfh(sig)
        # force re-serialization
        txin.script_sig = None
        txin.witness = None
        self.invalidate_ser_cache()

    #TODO: Move asset info to here
    def add_info_from_wallet(
            self,
            wallet: 'Abstract_Wallet',
            *,
            include_xpubs: bool = False,
            ignore_network_issues: bool = True,
    ) -> None:
        if self.is_complete():
            return
        # only include xpubs for multisig wallets; currently only they need it in practice
        # note: coldcard fw have a limitation that if they are included then all
        #       inputs are assumed to be multisig... https://github.com/spesmilo/electrum/pull/5440#issuecomment-549504761
        # note: trezor plugin needs xpubs included, if there are multisig inputs/change_outputs
        from .wallet import Multisig_Wallet
        if include_xpubs and isinstance(wallet, Multisig_Wallet):
            from .keystore import Xpub
            for ks in wallet.get_keystores():
                if isinstance(ks, Xpub):
                    fp_bytes, der_full = ks.get_fp_and_derivation_to_be_used_in_partial_tx(
                        der_suffix=[], only_der_suffix=False)
                    xpub = ks.get_xpub_to_be_used_in_partial_tx(only_der_suffix=False)
                    bip32node = BIP32Node.from_xkey(xpub)
                    self.xpubs[bip32node] = (fp_bytes, der_full)
        for txin in self.inputs():
            wallet.add_input_info(
                txin,
                only_der_suffix=False,
                ignore_network_issues=ignore_network_issues,
            )
        for txout in self.outputs():
            wallet.add_output_info(
                txout,
                only_der_suffix=False,
            )

    def remove_xpubs_and_bip32_paths(self) -> None:
        self.xpubs.clear()
        for txin in self.inputs():
            txin.bip32_paths.clear()
        for txout in self.outputs():
            txout.bip32_paths.clear()

    def prepare_for_export_for_coinjoin(self) -> None:
        """Removes all sensitive details."""
        # globals
        self.xpubs.clear()
        self._unknown.clear()
        # inputs
        for txin in self.inputs():
            txin.bip32_paths.clear()
        # outputs
        for txout in self.outputs():
            txout.redeem_script = None
            txout.witness_script = None
            txout.bip32_paths.clear()
            txout._unknown.clear()

    def convert_all_utxos_to_witness_utxos(self) -> None:
        """Replaces all NON-WITNESS-UTXOs with WITNESS-UTXOs.
        This will likely make an exported PSBT invalid spec-wise,
        but it makes e.g. QR codes significantly smaller.
        """
        for txin in self.inputs():
            txin.convert_utxo_to_witness_utxo()

    def remove_signatures(self):
        for txin in self.inputs():
            txin.part_sigs = {}
            txin.script_sig = None
            txin.witness = None
        assert not self.is_complete()
        self.invalidate_ser_cache()

    def update_txin_script_type(self):
        """Determine the script_type of each input by analyzing the scripts.
        It updates all tx-Inputs, NOT only the wallet owned, if the
        scriptpubkey is present.
        """
        for txin in self.inputs():
            if txin.script_type in ('unknown', 'address'):
                txin.set_script_type()

def pack_bip32_root_fingerprint_and_int_path(xfp: bytes, path: Sequence[int]) -> bytes:
    if len(xfp) != 4:
        raise Exception(f'unexpected xfp length. xfp={xfp}')
    return xfp + b''.join(i.to_bytes(4, byteorder='little', signed=False) for i in path)


def unpack_bip32_root_fingerprint_and_int_path(path: bytes) -> Tuple[bytes, Sequence[int]]:
    if len(path) % 4 != 0:
        raise Exception(f'unexpected packed path length. path={path.hex()}')
    xfp = path[0:4]
    int_path = [int.from_bytes(b, byteorder='little', signed=False) for b in chunks(path[4:], 4)]
    return xfp, int_path
