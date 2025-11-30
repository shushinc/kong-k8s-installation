Pricing profile:
Client is created or updated on the moriarty, i will post client billing profile to the aggragates.

Client Profie JSON:
{
    clientname: "twilio",
    type: : "demandpartner" or "enterprise",
    pricingtype: "domestic" or "internation"
}

I will store the vlaues in the redis microservice running at the same time,

When the aggragates receives the api request in the log, it will looka=for the pricingtype and value and sotre in the cache _client_pricing_cache[client] = (pricing_type, now). if the