# PresenceStub for reuse
class DummyXML:
    def findall(self, *args, **kwargs):
        return []

class PresenceStub(dict):
    """
    A test double for a Slixmpp Presence stanza that supports both
    dict and attribute-style access, for compatibility with MUC plugins.
    Usage:
        p = PresenceStub(from_=jid_obj, muc=muc_obj, type="available")
        # use p["from"], p.from_, p.xml, p["xml"], etc.
    """
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Populate both key and attribute for every kwarg
        for k, v in kwargs.items():
            setattr(self, k, v)
        # alias for handler code that uses pres.from instead of pres.from_
        if "from_" in kwargs:
            setattr(self, "from", kwargs["from_"])
            self["from"] = kwargs["from_"]
        # always provide a fake XML
        if "xml" not in kwargs:
            self.xml = DummyXML()
            self["xml"] = self.xml

    def __getattr__(self, item):
        # allow attribute-style fallback for dict keys
        try:
            return self[item]
        except KeyError:
            raise AttributeError(item)

    def __setattr__(self, key, value):
        super().__setattr__(key, value)
        # keep dict and attribute access in sync
        if key not in self:
            self[key] = value

    def __getitem__(self, item):
        # allow ['from'] to map to from_
        if item == "from" and "from_" in self:
            return self["from_"]
        return super().__getitem__(item)
