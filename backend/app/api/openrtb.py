"""OpenRTB 2.5 endpoints: the bid itself, and the notices that settle it.

An exchange posts a bid request and gets a bid response, or 204 No Content
when there is no bid, as the spec has it. It then calls the bid's nurl when
the bid wins, with the clearing price substituted for ${AUCTION_PRICE}, and
its lurl when it loses. The ad markup routes clicks through /click.
"""
import csv
import io
import json
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_autocommit_db, get_db
from app.models.models import ExchangeBid
from app.schemas import openrtb as ortb
from app.services.openrtb import Bidder, price_to_micros

router = APIRouter()

OPENRTB_VERSION = {"x-openrtb-version": "2.5"}
# Where the exchange should send notices and clicks. Behind a proxy the
# request's own base URL can come out as plain http, so it can be pinned.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")


@router.post("/2.5/bid", response_model=ortb.BidResponse, responses={204: {"description": "No bid"}})
async def bid(request: ortb.BidRequest, http: Request,
              db: AsyncSession = Depends(get_autocommit_db)):
    response = await Bidder(db).bid(request, PUBLIC_BASE_URL or str(http.base_url))
    if response is None:
        return Response(status_code=204, headers=OPENRTB_VERSION)
    return Response(content=response.model_dump_json(), media_type="application/json",
                    headers=OPENRTB_VERSION)


@router.get("/win/{bid_id}")
async def win(bid_id: str, price: str = Query(..., description="${AUCTION_PRICE}: CPM in USD"),
              db: AsyncSession = Depends(get_db)):
    try:
        micros = price_to_micros(price)
    except ValueError:
        # Includes a macro the exchange never substituted.
        raise HTTPException(status_code=400, detail=f"invalid price {price!r}")
    outcome = await Bidder(db).settle_win(bid_id, micros)
    if outcome == "unknown":
        raise HTTPException(status_code=404, detail="unknown bid")
    return {"bid_id": bid_id, "outcome": outcome}


@router.get("/loss/{bid_id}")
async def loss(bid_id: str, reason: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    code = int(reason) if reason and reason.isdigit() else None
    outcome = await Bidder(db).settle_loss(bid_id, code)
    if outcome == "unknown":
        raise HTTPException(status_code=404, detail="unknown bid")
    return {"bid_id": bid_id, "outcome": outcome}


@router.get("/click/{bid_id}")
async def click(bid_id: str, db: AsyncSession = Depends(get_db)):
    # The destination is the creative's own landing page, from the database;
    # nothing in the URL can choose it, so this is not an open redirect.
    destination = await Bidder(db).record_click(bid_id)
    if destination is None:
        raise HTTPException(status_code=404, detail="unknown bid")
    return RedirectResponse(destination, status_code=302)


@router.get("/stats")
async def stats(db: AsyncSession = Depends(get_db)):
    return await Bidder(db).stats()


LOG_COLUMNS = ["bid_id", "created_at", "strategy", "model_version", "pctr", "bid_cents",
               "status", "charged_micros", "clicked"]
FEATURE_COLUMNS = ["device_type", "country", "category", "category_source", "banner_pos",
                   "hour", "domain", "width", "height", "floor_cents", "auction_type",
                   "user_segments"]


@router.get("/log.csv")
async def training_log(limit: int = Query(10_000, ge=1, le=100_000),
                       db: AsyncSession = Depends(get_db)):
    """Every settled exchange bid with the model's inputs and the outcome.

    Won bids, labelled by whether they were clicked, are the training set
    for the next model: the features it saw, the prediction it made, and
    what then happened.
    """
    rows = (await db.execute(
        select(ExchangeBid).where(ExchangeBid.status != "pending")
        .order_by(ExchangeBid.created_at.desc()).limit(limit))).scalars().all()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(LOG_COLUMNS + FEATURE_COLUMNS)
    for b in rows:
        features = json.loads(b.features)
        segments = features.get("user_segments") or []
        features["user_segments"] = ",".join(segments)
        writer.writerow([b.id, b.created_at.isoformat(), b.strategy, b.model_version, b.pctr,
                         b.bid_cents, b.status, b.charged_micros, int(b.clicked)]
                        + [features.get(k) for k in FEATURE_COLUMNS])
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=exchange_bids.csv"})
