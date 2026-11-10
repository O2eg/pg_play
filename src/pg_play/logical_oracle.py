"""Independent exact comparator for quiescent migration experiments.

No application imports, production fingerprints or aggregate checksums. Row data
is streamed only by this optional experiment comparator; mismatch reports never contain row values.
Both writers must be stopped and replication drained before calling compare().
"""

import hashlib
from contextlib import AsyncExitStack


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def qualified(schema, name):
    return quote(schema) + "." + quote(name)


SCOPE = "n.nspname NOT IN ('lc','information_schema') AND n.nspname !~ '^pg_'"


def acl(expr):
    return (
        """(SELECT array_agg(concat_ws('|',pg_get_userbyid(grantor),
        CASE grantee WHEN 0 THEN 'PUBLIC' ELSE pg_get_userbyid(grantee) END,
        privilege_type,is_grantable) ORDER BY pg_get_userbyid(grantor),grantee=0,
        pg_get_userbyid(grantee),privilege_type,is_grantable) FROM aclexplode("""
        + expr
        + "))"
    )


RELATION_ACL = acl(
    """coalesce(c.relacl,acldefault(CASE WHEN c.relkind='S' THEN 's' ELSE 'r' END::"char",
        c.relowner))"""
)
QUERIES = {
    "schemas": f"""SELECT n.nspname,pg_get_userbyid(n.nspowner),
        {acl("coalesce(n.nspacl,acldefault('n',n.nspowner))")},obj_description(n.oid,'pg_namespace')
        FROM pg_namespace n WHERE {SCOPE} ORDER BY 1""",
    "relations": f"""SELECT n.nspname,c.relname,c.relkind::text,c.relpersistence::text,
        pg_get_userbyid(c.relowner),c.relreplident::text,c.reloptions,c.relispartition,
        pg_get_expr(c.relpartbound,c.oid),pg_get_partkeydef(c.oid),
        CASE WHEN c.relkind IN ('v','m') THEN pg_get_viewdef(c.oid) END,
        {RELATION_ACL},
        obj_description(c.oid,'pg_class')
        FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE {SCOPE} AND c.relkind IN ('r','p','v','m','S') ORDER BY 1,2""",
    "columns": f"""SELECT n.nspname,c.relname,a.attname,format_type(a.atttypid,a.atttypmod),
        a.attnotnull,a.attidentity::text,a.attgenerated::text,pg_get_expr(d.adbin,d.adrelid),
        cn.nspname,co.collname,a.attstorage::text,a.attstattarget,
        {acl("a.attacl")},col_description(c.oid,a.attnum)
        FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
        LEFT JOIN pg_collation co ON co.oid=a.attcollation
        LEFT JOIN pg_namespace cn ON cn.oid=co.collnamespace
        WHERE {SCOPE} AND c.relkind IN ('r','p','v','m')
        AND a.attnum>0 AND NOT a.attisdropped
        ORDER BY n.nspname,c.relname,a.attnum""",
    "constraints": f"""SELECT n.nspname,c.relname,k.conname,
        pg_get_constraintdef(k.oid),k.convalidated,
        k.condeferrable,k.condeferred,obj_description(k.oid,'pg_constraint')
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE {SCOPE} ORDER BY 1,2,3""",
    "indexes": f"""SELECT n.nspname,t.relname,c.relname,pg_get_indexdef(c.oid),
        i.indisvalid,i.indisready,
        i.indisreplident,i.indisclustered,c.reloptions,obj_description(c.oid,'pg_class')
        FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_class t ON t.oid=i.indrelid

        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE {SCOPE} ORDER BY 1,2,3""",
    "parents": f"""SELECT n.nspname,c.relname,pn.nspname,p.relname,h.inhseqno
        FROM pg_inherits h JOIN pg_class c ON c.oid=h.inhrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_class p ON p.oid=h.inhparent JOIN pg_namespace pn ON pn.oid=p.relnamespace
        WHERE {SCOPE} ORDER BY 1,2,5""",
    "functions": f"""SELECT n.nspname,p.proname,pg_get_function_identity_arguments(p.oid),
        pg_get_functiondef(p.oid),pg_get_userbyid(p.proowner),{acl("coalesce(p.proacl,acldefault('f',p.proowner))")},
        obj_description(p.oid,'pg_proc') FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE {SCOPE} AND p.prokind IN ('f','p') ORDER BY 1,2,3""",
    "triggers": f"""SELECT n.nspname,c.relname,t.tgname,pg_get_triggerdef(t.oid),t.tgenabled::text,
        obj_description(t.oid,'pg_trigger') FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid

        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE {SCOPE} AND NOT t.tgisinternal ORDER BY 1,2,3""",
    "types": f"""SELECT n.nspname,t.typname,t.typtype::text,pg_get_userbyid(t.typowner),
        format_type(t.typbasetype,t.typtypmod),t.typnotnull,t.typdefault,
        {acl("coalesce(t.typacl,acldefault('T',t.typowner))")},
        (SELECT array_agg(enumlabel ORDER BY enumsortorder) FROM pg_enum WHERE enumtypid=t.oid),
        (SELECT array_agg(pg_get_constraintdef(oid) ORDER BY conname)
         FROM pg_constraint WHERE contypid=t.oid),
        obj_description(t.oid,'pg_type') FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
        WHERE {SCOPE} AND t.typtype IN ('e','d') ORDER BY 1,2""",
    "sequence_definitions": f"""SELECT n.nspname,c.relname,format_type(s.seqtypid,NULL),s.seqstart,
        s.seqincrement,s.seqmax,s.seqmin,s.seqcache,s.seqcycle
        FROM pg_sequence s JOIN pg_class c ON c.oid=s.seqrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE {SCOPE} ORDER BY 1,2""",
    "sequence_ownership": f"""SELECT n.nspname,c.relname,pn.nspname,p.relname,
        a.attname,d.deptype::text
        FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_depend d ON d.classid='pg_class'::regclass AND d.objid=c.oid
            AND d.refclassid='pg_class'::regclass AND d.deptype IN ('a','i')
        JOIN pg_class p ON p.oid=d.refobjid JOIN pg_namespace pn ON pn.oid=p.relnamespace
        JOIN pg_attribute a ON a.attrelid=p.oid AND a.attnum=d.refobjsubid
        WHERE {SCOPE} AND c.relkind='S' ORDER BY 1,2,3,4,5""",
    "default_acl": """SELECT pg_get_userbyid(d.defaclrole),n.nspname,d.defaclobjtype::text,
        """
    + acl("d.defaclacl")
    + """ FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace
        WHERE n.nspname IS NULL OR n.nspname<>'lc' ORDER BY 1,2,3""",
    "database": """SELECT pg_get_userbyid(datdba),encoding,datcollate,datctype,datconnlimit,
        """
    + acl("coalesce(datacl,acldefault('d',datdba))")
    + """,obj_description(oid,'pg_database')
        FROM pg_database WHERE datname=current_database()""",
    "settings": """SELECT CASE setrole WHEN 0 THEN 'ALL' ELSE pg_get_userbyid(setrole) END,setconfig
        FROM pg_db_role_setting WHERE setdatabase=(SELECT oid FROM pg_database
        WHERE datname=current_database()) ORDER BY 1""",
    "extensions": """SELECT e.extname,e.extversion,n.nspname,pg_get_userbyid(e.extowner)
        FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace ORDER BY 1""",
}


async def catalog(connection, executor):
    result = {key: [tuple(r) for r in await connection.fetch(sql)] for key, sql in QUERIES.items()}
    result["roles"] = []
    for r in await connection.fetch(
        """SELECT rolname,rolsuper,rolinherit,rolcreaterole,rolcreatedb,rolcanlogin,
        rolreplication,rolbypassrls,rolconnlimit,rolvaliduntil::text,
        (SELECT setconfig FROM pg_db_role_setting WHERE setrole=a.oid AND setdatabase=0),rolpassword
        FROM pg_authid a WHERE rolname !~ '^pg_' AND rolname<>$1 ORDER BY rolname""",
        executor,
    ):
        values = list(r)
        password = values.pop()
        values.append(None if password is None else hashlib.sha256(password.encode()).hexdigest())
        result["roles"].append(tuple(values))
    result["memberships"] = [
        tuple(r)
        for r in await connection.fetch(
            """SELECT pg_get_userbyid(roleid),
        pg_get_userbyid(member),pg_get_userbyid(grantor),admin_option,
        to_jsonb(m)->>'inherit_option',to_jsonb(m)->>'set_option' FROM pg_auth_members m
        WHERE $1 NOT IN (pg_get_userbyid(roleid),
        pg_get_userbyid(member),pg_get_userbyid(grantor)) ORDER BY 1,2,3""",
            executor,
        )
    ]
    return result


async def exact_stream(left, right, sql, *, label, right_sql=None, prefetch=256):
    streams = [
        c.cursor(query, prefetch=prefetch).__aiter__()
        for c, query in ((left, sql), (right, right_sql or sql))
    ]
    sentinel, count = object(), 0
    while True:
        a, b = await anext(streams[0], sentinel), await anext(streams[1], sentinel)
        if a is sentinel and b is sentinel:
            return count
        if a is sentinel or b is sentinel or tuple(a) != tuple(b):
            raise AssertionError(f"Independent oracle: {label} differs at row {count}")
        count += 1


async def compare(source, target, *, executor="", deferred=False):
    async with AsyncExitStack() as stack:
        for c in (source, target):
            options = (
                {} if c.is_in_transaction() else dict(isolation="repeatable_read", readonly=True)
            )
            await stack.enter_async_context(c.transaction(**options))
            await c.execute(
                "SET LOCAL search_path=pg_catalog; SET LOCAL timezone='UTC'; "
                "SET LOCAL datestyle='ISO,YMD'; SET LOCAL intervalstyle='postgres'; "
                "SET LOCAL extra_float_digits=3"
            )
        a, b = await catalog(source, executor), await catalog(target, executor)
        changes = [key for key in a if a[key] != b[key]]
        if changes:
            raise AssertionError("Independent oracle: catalog differs: " + ", ".join(changes))
        tables = await source.fetch(
            f"""SELECT c.oid,n.nspname,c.relname,c.relkind::text,c.relispopulated
            FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE {SCOPE}
            AND NOT c.relispartition AND c.relkind IN ('r','p','m')
            AND ($1 OR (c.relpersistence='p' AND c.relkind<>'m')) ORDER BY 2,3""",
            deferred,
        )
        counts = {}
        for row in tables:
            name = qualified(row["nspname"], row["relname"])
            if row["relkind"] == "m":
                populated = await target.fetchval(
                    "SELECT relispopulated FROM pg_class WHERE oid=to_regclass($1)", name
                )
                if populated != row["relispopulated"]:
                    raise AssertionError("Independent oracle: matview population differs")
                if not populated:
                    continue
            keys = await source.fetch(
                """SELECT a.attname FROM pg_index i
                CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum,position)
                JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum
                WHERE i.indrelid=$1 AND i.indisprimary AND k.position<=i.indnkeyatts
                ORDER BY k.position""",
                row["oid"],
            )
            order = (
                ",".join("t." + quote(k["attname"]) for k in keys)
                if keys
                else 'row_to_json(t)::text COLLATE "C"'
            )
            target_sql = "SELECT row_to_json(t)::text FROM " + name + " t ORDER BY " + order
            source_sql = target_sql
            if row["relkind"] == "m":
                # Cutover refreshes the target. The source's stored materialized
                # result may be stale; evaluate its definition independently.
                definition = await source.fetchval("SELECT pg_get_viewdef($1::oid)", row["oid"])
                source_sql = (
                    "SELECT row_to_json(t)::text FROM ("
                    + definition.rstrip(";\n ")
                    + ") t ORDER BY "
                    + order
                )
            counts[name] = await exact_stream(
                source,
                target,
                source_sql,
                right_sql=target_sql,
                label=name,
            )
        if deferred:
            for row in a["sequence_definitions"]:
                name = qualified(row[0], row[1])
                if tuple(
                    await source.fetchrow("SELECT last_value,is_called FROM " + name)
                ) != tuple(await target.fetchrow("SELECT last_value,is_called FROM " + name)):
                    raise AssertionError("Independent oracle: sequence value differs: " + name)
            metadata = (
                "SELECT oid,pg_get_userbyid(lomowner),"
                + acl("coalesce(lomacl,acldefault('L',lomowner))")
                + ",obj_description(oid,'pg_largeobject') FROM pg_largeobject_metadata ORDER BY oid"
            )
            await exact_stream(source, target, metadata, label="large object metadata")
            await exact_stream(
                source,
                target,
                "SELECT loid,pageno,data FROM pg_largeobject ORDER BY loid,pageno",
                label="large object pages",
            )
        return dict(
            passed=True, comparator="independent-exact-stream", tables=counts, deferred=deferred
        )
