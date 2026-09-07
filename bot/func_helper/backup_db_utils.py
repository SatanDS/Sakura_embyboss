import asyncio
import glob
import os
from datetime import datetime

from bot import LOGGER


class BackupDBUtils:

    @staticmethod
    # 数据库备份(mysql直装/本机含有mysql)
    async def backup_mysql_db(host, port, user, password, database_name, backup_dir, max_backup_count):
        # 如果文件夹不存在，就创建它
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir)
        # 根据时间创建当前备份文件
        backup_file = os.path.join(backup_dir, f'{database_name}-{datetime.now().strftime("%Y-%m-%d-%H-%M-%S")}.sql')
        return_code = -1
        try:
            mysql_env = os.environ.copy()
            mysql_env["MYSQL_PWD"] = password
            with open(backup_file, "wb") as output:
                process = await asyncio.create_subprocess_exec(
                    "mysqldump", f"-h{host}", f"-P{port}", f"-u{user}",
                    "--no-tablespaces", database_name, env=mysql_env,
                    stdout=output,
                )
                await process.communicate()
            return_code = process.returncode
            if return_code != 0:
                LOGGER.warning(f"BOT数据库备份失败，使用 skip-ssl方式尝试备份")
                with open(backup_file, "wb") as output:
                    process = await asyncio.create_subprocess_exec(
                        "mysqldump", f"-h{host}", f"-P{port}", f"-u{user}",
                        "--skip-ssl", "--no-tablespaces", database_name, env=mysql_env,
                        stdout=output,
                    )
                    await process.communicate()
                return_code = process.returncode
            if return_code != 0:
                LOGGER.error(f"BOT数据库备份失败, error code: {return_code}")
                if os.path.exists(backup_file):
                    os.remove(backup_file)
                return None
            LOGGER.info(f"BOT数据库备份成功,文件保存为 {backup_file}")
            # 获取所有备份文件，并且通过时间进行排序
            all_backups = sorted(glob.glob(os.path.join(backup_dir, f'{database_name}-*.sql')))
            # 如果超过了当前的备份最大数量，则删除最久的一个
            while len(all_backups) > max_backup_count:
                os.remove(all_backups[0])
                all_backups.pop(0)
        except Exception as e:
            LOGGER.error(f"BOT数据库备份失败, error: {str(e)}")
            return None
        return backup_file

    @staticmethod
    # 数据库备份(docker)
    async def backup_mysql_db_docker(container_name, user, password, database_name, backup_dir, max_backup_count):
        # 如果文件夹不存在，就创建它
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir)
        # 根据当前时间创建备份文件
        backup_file_on_host = os.path.join(
            backup_dir,
            f'{database_name}-{datetime.now().strftime("%Y-%m-%d-%H-%M-%S")}.sql',
        )
        # 进入容器，使用mysqldump备份文件
        return_code = -1
        try:
            docker_mysql_env = ["-e", f"MYSQL_PWD={password}"]
            with open(backup_file_on_host, "wb") as output:
                process = await asyncio.create_subprocess_exec(
                    "docker", "exec", *docker_mysql_env, container_name, "mysqldump",
                    "--no-tablespaces", f"-u{user}", database_name,
                    stdout=output,
                )
                await process.communicate()
            return_code = process.returncode
            if return_code != 0:
                LOGGER.warning(f"BOT数据库备份失败，使用 skip-ssl方式尝试备份")
                with open(backup_file_on_host, "wb") as output:
                    process = await asyncio.create_subprocess_exec(
                        "docker", "exec", *docker_mysql_env, container_name, "mysqldump", "--skip-ssl",
                        "--no-tablespaces", f"-u{user}", database_name,
                        stdout=output,
                    )
                    await process.communicate()
                return_code = process.returncode
            if return_code != 0:
                LOGGER.error(f"BOT数据库备份失败, error code: {return_code}")
                if os.path.exists(backup_file_on_host):
                    os.remove(backup_file_on_host)
                return None
        except Exception as e:
            LOGGER.error(f"BOT数据库备份失败, error: {str(e)}")
            if os.path.exists(backup_file_on_host):
                os.remove(backup_file_on_host)
            return None
        LOGGER.info(f"BOT数据库备份成功,文件保存为 {backup_file_on_host}")
        # 获取所有备份文件，并且通过时间进行排序
        all_backups = sorted(glob.glob(os.path.join(backup_dir, f'{database_name}-*.sql')))
        # 如果超过了当前的备份最大数量，则删除最久的一个
        while len(all_backups) > max_backup_count:
            os.remove(all_backups[0])
            all_backups.pop(0)
        return backup_file_on_host
