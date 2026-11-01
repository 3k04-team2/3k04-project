"""M2 Message Codec.

Secret: the wire format, the checksum algorithm and the protocol revision
(JSON with CRC-32 now). Turns bytes into Message objects and back.
Uses: no other module.
"""

import json
import zlib

REVISION = "1"


class ChecksumError(Exception):
    pass


class FormatError(Exception):
    pass


class UnsupportedRevision(Exception):
    pass


class Message:
    def __init__(self, type, adc="", seq=0, time=0.0, body=None, revision=REVISION):
        self.type = type
        self.adc = adc
        self.seq = seq
        self.time = time
        self.body = body if body is not None else {}
        self.revision = revision

    def __repr__(self):
        return "Message(%s adc=%s seq=%s %s)" % (self.type, self.adc, self.seq, self.body)


class MessageCodec:
    def revision(self):
        return REVISION

    @staticmethod
    def _crc(frame):
        text = json.dumps(frame, sort_keys=True, separators=(",", ":"))
        return zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF

    def encode(self, msg):
        frame = {"rev": msg.revision, "adc": msg.adc, "seq": msg.seq,
                 "time": msg.time, "type": msg.type, "body": msg.body}
        wrapper = {"frame": frame, "crc": self._crc(frame)}
        return json.dumps(wrapper, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def decode(self, data):
        try:
            wrapper = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise FormatError("not valid JSON")
        if not isinstance(wrapper, dict) or "frame" not in wrapper or "crc" not in wrapper:
            raise FormatError("missing frame or checksum")
        frame = wrapper["frame"]
        if not isinstance(frame, dict):
            raise FormatError("frame is not an object")
        if self._crc(frame) != wrapper["crc"]:
            raise ChecksumError("checksum does not match")
        if frame.get("rev") != REVISION:
            raise UnsupportedRevision("revision %r" % (frame.get("rev"),))
        ok = (isinstance(frame.get("adc"), str)
              and isinstance(frame.get("seq"), int) and not isinstance(frame.get("seq"), bool)
              and isinstance(frame.get("time"), (int, float)) and not isinstance(frame.get("time"), bool)
              and isinstance(frame.get("type"), str)
              and isinstance(frame.get("body"), dict))
        if not ok:
            raise FormatError("a field is missing or has the wrong type")
        return Message(frame["type"], frame["adc"], frame["seq"], frame["time"],
                       frame["body"], frame["rev"])
