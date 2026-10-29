"""The parts of OpenRTB 2.5 a display bidder reads and writes.

Exchanges send far more than this, and versions add fields; anything not
listed is accepted and ignored rather than rejected, as the spec asks of a
bidder. Field names and meanings follow the IAB OpenRTB 2.5 specification.
"""
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class Format(_Open):
    w: int
    h: int


class Banner(_Open):
    w: Optional[int] = None
    h: Optional[int] = None
    format: list[Format] = Field(default_factory=list)
    pos: Optional[int] = None          # 5.4 ad position: 1 = above the fold


class Imp(_Open):
    id: str
    banner: Optional[Banner] = None    # video and native are not bid on
    bidfloor: float = 0.0              # CPM, in bidfloorcur
    bidfloorcur: str = "USD"
    tagid: Optional[str] = None


class Publisher(_Open):
    id: Optional[str] = None
    name: Optional[str] = None


class Site(_Open):
    id: Optional[str] = None
    domain: Optional[str] = None
    page: Optional[str] = None
    cat: list[str] = Field(default_factory=list)       # IAB content categories
    publisher: Optional[Publisher] = None


class Geo(_Open):
    country: Optional[str] = None      # ISO-3166-1 alpha-3 in 2.5


class Device(_Open):
    ua: Optional[str] = None
    devicetype: Optional[int] = None   # 5.21
    geo: Optional[Geo] = None


class Segment(_Open):
    id: Optional[str] = None


class Data(_Open):
    id: Optional[str] = None
    segment: list[Segment] = Field(default_factory=list)


class User(_Open):
    id: Optional[str] = None
    data: list[Data] = Field(default_factory=list)


class BidRequest(_Open):
    id: str
    imp: list[Imp]
    site: Optional[Site] = None
    device: Optional[Device] = None
    user: Optional[User] = None
    at: int = 2                        # 1 first price, 2 second price
    tmax: Optional[int] = None         # ms the exchange will wait
    cur: list[str] = Field(default_factory=list)
    bcat: list[str] = Field(default_factory=list)   # blocked content categories
    badv: list[str] = Field(default_factory=list)   # blocked advertiser domains
    test: int = 0


class Bid(BaseModel):
    id: str
    impid: str
    price: float                       # CPM, in the response currency
    adid: str
    crid: str
    cid: str
    adomain: list[str]
    w: int
    h: int
    adm: str
    nurl: str
    lurl: str


class SeatBid(BaseModel):
    seat: str
    bid: list[Bid]


class BidResponse(BaseModel):
    id: str
    bidid: str
    cur: str = "USD"
    seatbid: list[SeatBid]
