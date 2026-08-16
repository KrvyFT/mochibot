---
name: weather
description: 查看配置地点的当前天气
type: hybrid
requires:
  env: [WEATHER_CITY]
sense:
  interval: 60
config:
  WEATHER_CITY:
    type: str
    default: ""
    description: "City name (e.g. Tokyo, New York, Shanghai)"
---

## Tools

### get_weather (routed)
查看配置地点的当前天气实况，包括温度、降水和体感；不提供未来预报。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| force_refresh | boolean | no | 需要最新实况时设为 true；否则可以使用最近结果 |
