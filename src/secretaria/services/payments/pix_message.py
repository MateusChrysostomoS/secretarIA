"""The Pix "copia e cola" as part of ONE patient message (2026-10-27).

A Pix request used to travel in two messages: the explanation and, right after, the bare code
(so it was easy to copy). Now it is ONE message and the code is a fenced block at its end:

    ...explanation...
    Pix copia e cola:
    ```00020126...```

The triple backtick is the SAME marker on both channels, so no schema or contract changes:

* WhatsApp renders ```text``` as monospace (and a long press copies it);
* the Portal front recognises the fence in a bot message and draws a grey, click-to-copy code
  box (Brain-Message-Frontend `splitPixCode`). A message without a fence - every row written
  before this change - is drawn exactly as it always was.

A Pix code is base-36 EMV text: it never contains a backtick, so the fence cannot be broken.
"""

FENCE = "```"
PIX_CODE_LABEL = "Pix copia e cola:"


def pix_code_block(copy_paste: str) -> str:
    """The code as the fenced block both channels understand."""
    return f"{FENCE}{copy_paste.strip().replace(FENCE, '')}{FENCE}"


def pix_code_tail(copy_paste: str | None) -> list[str]:
    """The closing lines of a Pix message (label + fenced code); empty when there is no code."""
    if not copy_paste or not copy_paste.strip():
        return []
    return [PIX_CODE_LABEL, pix_code_block(copy_paste)]
