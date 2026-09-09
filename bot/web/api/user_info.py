#! /usr/bin/python3
# -*- coding: utf-8 -*-
"""
get_user_info -
Author:susu
Date:2024/8/27
"""

import json
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from bot.sql_helper.sql_emby import Emby, sql_get_emby, sql_update_emby
from bot.func_helper.emby import emby
from bot import LOGGER, group, bot

route = APIRouter()


@route.get("/user_info")
async def user_info(tg: str):
    # 从数据库获取用户信息
    user = sql_get_emby(tg)

    if not user:
        return JSONResponse(status_code=404, content={"code": 404, "message": "用户不存在"})
    return {"code": 200, "data": {"tg": user.tg, "iv": user.iv, "name": user.name, "embyid": user.embyid, "lv": user.lv, "cr": user.cr, "ex": user.ex}}


@route.post("/update_credit")
async def update_credit(request: Request):
    """
    修改用户积分
    :param request: 请求对象
    """
    try:
        content_type = request.headers.get("content-type", "").lower()
        if "application/json" in content_type:
            data = await request.json()
            if isinstance(data, str):
                data = json.loads(data)
        else:
            form_data = await request.form()
            data = json.loads(form_data["data"]) if "data" in form_data else {}

        if not isinstance(data, dict):
            return JSONResponse(status_code=400, content={"code": 400, "message": "参数错误"})
        tg = data.get("tg")
        credit = data.get("credit")
        if not isinstance(tg, (str, int)) or isinstance(tg, bool) or not tg or credit is None:
            return JSONResponse(status_code=400, content={"code": 400, "message": "参数错误"})
        if isinstance(credit, bool) or not isinstance(credit, (str, int)):
            return JSONResponse(status_code=400, content={"code": 400, "message": "积分必须是整数"})
        try:
            credit = int(credit)
        except ValueError:
            return JSONResponse(status_code=400, content={"code": 400, "message": "积分必须是整数"})

        # 获取用户信息
        user = sql_get_emby(tg)
        if not user:
            return JSONResponse(status_code=404, content={"code": 404, "message": "用户不存在"})

        # 计算新的积分值
        new_iv = user.iv + credit
        if new_iv < 0:
            return JSONResponse(status_code=400, content={"code": 400, "message": "积分不足"})
        # 更新用户积分
        user.iv = new_iv
        res = sql_update_emby(Emby.tg == tg, iv=new_iv)
        if res:

            return {
                "code": 200,
                "data": {"tg": user.tg, "iv": user.iv, "changed": credit},
            }
        else:
            return JSONResponse(status_code=500, content={"code": 500, "message": "更新失败"})
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"code": 400, "message": "无效的JSON格式"})
    except Exception as e:
        LOGGER.error(f"Credit update failed: {type(e).__name__}")
        return JSONResponse(status_code=500, content={"code": 500, "message": "服务器错误"})

@route.post("/ban")
async def ban_user(request: Request):
    """
    封禁用户
    :param request: 请求对象
    """
    try:
        content_type = request.headers.get("content-type", "").lower()
        if "application/json" in content_type:
            data = await request.json()
            if isinstance(data, str):
                data = json.loads(data)
        else:
            form_data = await request.form()
            data = json.loads(form_data["data"]) if "data" in form_data else {}

        if not isinstance(data, dict):
            return JSONResponse(status_code=400, content={"code": 400, "message": "参数错误"})
        query = data.get("query")
        if not isinstance(query, (str, int)) or isinstance(query, bool) or not query:
            return JSONResponse(status_code=400, content={"code": 400, "message": "参数错误"})

        # 获取用户信息 query 可以是 tg 或 embyname 或 embyid
        user = sql_get_emby(tg = query)
        if not user or not user.embyid:
            return JSONResponse(status_code=404, content={"code": 404, "message": "用户不存在"})
        
        disable_emby = await emby.emby_change_policy(emby_id=user.embyid, disable=True)
        
        if disable_emby:
            # 更新用户等级为封禁状态
            user.lv = 'c'  # 封禁状态
            if not sql_update_emby(Emby.tg == user.tg, lv='c'):
                return JSONResponse(status_code=503, content={"code": 503, "message": "本地封禁状态更新失败"})
            send_notification = f"#BAN通告\n用户 {user.name} (TG: #{user.tg}, EmbyID: {user.embyid}) 已被封禁。"
            LOGGER.info(send_notification)
            try:
                await bot.send_message(chat_id=group[0], text=send_notification)
            except Exception as exc:
                LOGGER.warning(f"Account ban notification failed: {type(exc).__name__}")
            return {
                "code": 200,
                "data": {"tg": user.tg,"embyid": user.embyid, "name": user.name, "lv": user.lv},
            }
        else:
            return JSONResponse(status_code=502, content={"code": 502, "message": "封禁失败"})
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"code": 400, "message": "无效的JSON格式"})
    except Exception as e:
        LOGGER.error(f"Account ban failed: {type(e).__name__}")
        return JSONResponse(status_code=500, content={"code": 500, "message": "服务器错误"})
