#! /usr/bin/python3
# -*- coding: utf-8 -*-
"""
login.py - Emby用户登录接口
Author: susu
Date: 2025/01/06
"""

import json
from typing import Optional
from fastapi import APIRouter, Request 
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from bot.func_helper.emby import emby
from bot.sql_helper.sql_emby import Emby, sql_get_emby
from bot import LOGGER

router = APIRouter()


class LoginRequest(BaseModel):
    """登录请求模型"""
    username: str
    password: str


class LoginResponse(BaseModel):
    """登录响应模型"""
    code: int
    message: str
    data: Optional[dict] = None


def login_error(code: int, message: str):
    return JSONResponse(status_code=code, content=LoginResponse(code=code, message=message).model_dump())


@router.post("/login", response_model=LoginResponse)
async def login(request: Request):
    """
    Emby用户登录接口
    
    Request Body:
    {
        "username": "user_name",
        "password": "user_password"
    }
    
    Success Response (200):
    {
        "code": 200,
        "message": "登录成功",
        "data": {
            "embyid": "user_id",
            "username": "user_name"
        }
    }
    
    Error Response:
    {
        "code": 401,
        "message": "用户名或密码错误"
    }
    """
    try:
        # 获取请求数据
        content_type = request.headers.get("content-type", "").lower()
        
        if "application/json" in content_type:
            data = await request.json()
            if isinstance(data, str):
                data = json.loads(data)
        else:
            form_data = await request.form()
            data = json.loads(form_data.get("data", "{}")) if "data" in form_data else dict(form_data)
        
        # 验证必要参数
        if not isinstance(data, dict):
            return login_error(400, "参数错误")
        username = data.get("username")
        password = data.get("password")
        if not isinstance(username, str) or (password is not None and not isinstance(password, str)):
            return login_error(400, "参数错误")
        
        if not username:
            LOGGER.warning(f"Login attempt missing credentials")
            return login_error(
                code=400,
                message="缺少用户名"
            )
        embyindb = sql_get_emby(username)
        if not embyindb:
            LOGGER.warning(f"Login attempt for non-existent user: {username}")
            return login_error(
                code=404,
                message="用户不存在"
            )
        
        # 调用Emby API进行身份验证
        success, embyid = await emby.authority_account(
            tg_id=0,
            username=username,
            password=password
        )
        
        if not success:
            LOGGER.warning(f"Login failed for user: {username}")
            return login_error(
                code=401,
                message="用户名或密码错误"
            )
        
        LOGGER.info(f"User logged in successfully: {username}")
        
        return LoginResponse(
            code=200,
            message="登录成功",
            data={
                "embyid": str(embyid),
                "username": username
            }
        )
        
    except json.JSONDecodeError:
        LOGGER.error(f"Invalid JSON format in login request")
        return login_error(
            code=400,
            message="无效的JSON格式"
        )
    except Exception as e:
        LOGGER.error(f"Login error: {str(e)}")
        return login_error(
            code=500,
            message="服务器错误"
        )
