# Please leave the "<vCard xmlns=\"vcard-temp\">" and "</vCard>" tags
# in place, otherwise the bot won't recognize the file as a vCard. You
# can also add more fields, but the bot won't show them.
VCARD = """<vCard xmlns=\"vcard-temp\">
  <FN>[botname] XMPP Testbot No.1</FN>
  <NICKNAME>[botnickname]</NICKNAME>
  <BDAY>2026-03-05</BDAY>
  <ORG>
    <ORGNAME>[botname] development center</ORGNAME>
    <ORGUNIT>XMPP server</ORGUNIT>
  </ORG>
  <EMAIL><USERID>admin@example.tld</USERID></EMAIL>
  <URL>https://git.envs.net/dan/envsbot</URL>
  <URL>https://github.com/dan-envs/envsbot</URL>
  <URL>https://your.home.page/</URL>
  <ADR>
    <LOCALITY>Anchorage</LOCALITY>
    <REGION>Alaska</REGION>
    <CTRY>USA</CTRY>
  </ADR>
  <NOTE>I'm a XMPP helper bot to serve you mainly in MUC rooms.</NOTE>
  <NOTE>My pronouns are 'it/its'.</NOTE>
  <NOTE>You can add as many NICKNAMEs, ORGs, EMAILs and URLs as you want. But
  the bot itself only uses the first ADR field and only LOCALITY, REGION and
  CTRY when displaying vCards. You also have to set the "timezone" field in the
  config.json file. The TZ field in your vCard file won't be recognized,
  although you can set it.</NOTE>
  <NOTE>You can also add other fields, which can be expected in a XMPP vCard,
but the bot won't show them.</NOTE>
</vCard>"""
