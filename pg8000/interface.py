# vim: sw=4:expandtab:foldmethod=marker
#
# Copyright (c) 2007, Mathieu Fenniak
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#
# * Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.
# * Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
# * The name of the author may not be used to endorse or promote products
# derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

__author__ = "Mathieu Fenniak"

import socket
import protocol
import threading
from errors import *

class DataIterator(object):
    def __init__(self, obj, func):
        self.obj = obj
        self.func = func

    def __iter__(self):
        return self

    def __next__(self):
        retval = self.func(self.obj)
        if retval == None:
            raise StopIteration()
        return retval

##
# This class represents a prepared statement.  A prepared statement is
# pre-parsed on the server, which reduces the need to parse the query every
# time it is run.  The statement can have parameters in the form of $1, $2, $3,
# etc.  When parameters are used, the types of the parameters need to be
# specified when creating the prepared statement.
# <p>
# As of v1.01, instances of this class are thread-safe.  This means that a
# single PreparedStatement can be accessed by multiple threads without the
# internal consistency of the statement being altered.  However, the
# responsibility is on the client application to ensure that one thread reading
# from a statement isn't affected by another thread starting a new query with
# the same statement.
# <p>
# Stability: Added in v1.00, stability guaranteed for v1.xx.
#
# @param connection     An instance of {@link Connection Connection}.
#
# @param statement      The SQL statement to be represented, often containing
# parameters in the form of $1, $2, $3, etc.
#
# @param types          Python type objects for each parameter in the SQL
# statement.  For example, int, float, str.
class PreparedStatement(object):

    ##
    # Determines the number of rows to read from the database server at once.
    # Reading more rows increases performance at the cost of memory.  The
    # default value is 100 rows.  The affect of this parameter is transparent.
    # That is, the library reads more rows when the cache is empty
    # automatically.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.  It is
    # possible that implementation changes in the future could cause this
    # parameter to be ignored.O
    row_cache_size = 100

    def __init__(self, connection, statement, *types):
        self.c = connection.c
        self._portal_name = "pg8000_portal_%s_%s" % (id(self.c), id(self))
        self._statement_name = "pg8000_statement_%s_%s" % (id(self.c), id(self))
        self._row_desc = None
        self._cached_rows = []
        self._command_complete = True
        self._parse_row_desc = self.c.parse(self._statement_name, statement, types)
        self._lock = threading.RLock()

    def __del__(self):
        # This __del__ should work with garbage collection / non-instant
        # cleanup.  It only really needs to be called right away if the same
        # object id (and therefore the same statement name) might be reused
        # soon, and clearly that wouldn't happen in a GC situation.
        self.c.close_statement(self._statement_name)

    row_description = property(lambda self: self._getRowDescription())
    def _getRowDescription(self):
        if self._row_desc == None:
            return None
        return self._row_desc.fields

    ##
    # Run the SQL prepared statement with the given parameters.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.
    def execute(self, *args):
        self._lock.acquire()
        try:
            if not self._command_complete:
                # cleanup last execute
                self._cached_rows = []
                self.c.close_portal(self._portal_name)
            self._command_complete = False
            self._row_desc = self.c.bind(self._portal_name, self._statement_name, args, self._parse_row_desc)
            if self._row_desc:
                # We execute our cursor right away to fill up our cache.  This
                # prevents the cursor from being destroyed, apparently, by a rogue
                # Sync between Bind and Execute.  Since it is quite likely that
                # data will be read from us right away anyways, this seems a safe
                # move for now.
                self._fill_cache()
        finally:
            self._lock.release()

    def _fill_cache(self):
        self._lock.acquire()
        try:
            if self._cached_rows:
                raise InternalError("attempt to fill cache that isn't empty")
            end_of_data, rows = self.c.fetch_rows(self._portal_name, self.row_cache_size, self._row_desc)
            self._cached_rows = rows
            if end_of_data:
                self._command_complete = True
        finally:
            self._lock.release()

    def _fetch(self):
        self._lock.acquire()
        try:
            if not self._cached_rows:
                if self._command_complete:
                    return None
                self._fill_cache()
                if self._command_complete and not self._cached_rows:
                    # fill cache tells us the command is complete, but yet we have
                    # no rows after filling our cache.  This is a special case when
                    # a query returns no rows.
                    return None
            row = self._cached_rows.pop(0)
            return tuple(row)
        finally:
            self._lock.release()

    ##
    # Read a row from the database server, and return it in a dictionary
    # indexed by column name/alias.  This method will raise an error if two
    # columns have the same name.  Returns None after the last row.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.
    def read_dict(self):
        row = self._fetch()
        if row == None:
            return row
        retval = {}
        for i in range(len(self._row_desc.fields)):
            col_name = self._row_desc.fields[i]['name']
            if col_name in retval:
                raise InterfaceError("cannot return dict of row when two columns have the same name (%r)" % (col_name,))
            retval[col_name] = row[i]
        return retval

    ##
    # Read a row from the database server, and return it as a tuple of values.
    # Returns None after the last row.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.
    def read_tuple(self):
        row = self._fetch()
        if row == None:
            return row
        return row

    ##
    # Return an iterator for the output of this statement.  The iterator will
    # return a tuple for each row, in the same manner as {@link
    # #PreparedStatement.read_tuple read_tuple}.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.
    def iterate_tuple(self):
        return DataIterator(self, PreparedStatement.read_tuple)

    ##
    # Return an iterator for the output of this statement.  The iterator will
    # return a dict for each row, in the same manner as {@link
    # #PreparedStatement.read_dict read_dict}.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.
    def iterate_dict(self):
        return DataIterator(self, PreparedStatement.read_dict)

##
# The Cursor class allows multiple queries to be performed concurrently with a
# single PostgreSQL connection.  The Cursor object is implemented internally by
# using a {@link PreparedStatement PreparedStatement} object, so if you plan to
# use a statement multiple times, you might as well create a PreparedStatement
# and save a small amount of reparsing time.
# <p>
# As of v1.01, instances of this class are thread-safe.  See {@link
# PreparedStatement PreparedStatement} for more information.
# <p>
# Stability: Added in v1.00, stability guaranteed for v1.xx.
#
# @param connection     An instance of {@link Connection Connection}.
class Cursor(object):
    def __init__(self, connection):
        self.connection = connection
        self._stmt = None

    row_description = property(lambda self: self._getRowDescription())
    def _getRowDescription(self):
        if self._stmt == None:
            return None
        return self._stmt.row_description

    ##
    # Run an SQL statement using this cursor.  The SQL statement can have
    # parameters in the form of $1, $2, $3, etc., which will be filled in by
    # the additional arguments passed to this function.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.
    # @param query      The SQL statement to execute.
    def execute(self, query, *args):
        self._stmt = PreparedStatement(self.connection, query, *[type(x) for x in args])
        self._stmt.execute(*args)

    ##
    # Read a row from the database server, and return it in a dictionary
    # indexed by column name/alias.  This method will raise an error if two
    # columns have the same name.  Returns None after the last row.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.
    def read_dict(self):
        if self._stmt == None:
            raise ProgrammingError("attempting to read from unexecuted cursor")
        return self._stmt.read_dict()

    ##
    # Read a row from the database server, and return it as a tuple of values.
    # Returns None after the last row.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.
    def read_tuple(self):
        if self._stmt == None:
            raise ProgrammingError("attempting to read from unexecuted cursor")
        return self._stmt.read_tuple()

    ##
    # Return an iterator for the output of this statement.  The iterator will
    # return a tuple for each row, in the same manner as {@link
    # #PreparedStatement.read_tuple read_tuple}.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.
    def iterate_tuple(self):
        if self._stmt == None:
            raise ProgrammingError("attempting to read from unexecuted cursor")
        return self._stmt.iterate_tuple()

    ##
    # Return an iterator for the output of this statement.  The iterator will
    # return a dict for each row, in the same manner as {@link
    # #PreparedStatement.read_dict read_dict}.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.
    def iterate_dict(self):
        if self._stmt == None:
            raise ProgrammingError("attempting to read from unexecuted cursor")
        return self._stmt.iterate_dict()

##
# This class represents a connection to a PostgreSQL database.
# <p>
# The database connection is derived from the {@link #Cursor Cursor} class,
# which provides a default cursor for running queries.  It also provides
# transaction control via the 'begin', 'commit', and 'rollback' methods.
# Without beginning a transaction explicitly, all statements will autocommit to
# the database.
# <p>
# As of v1.01, instances of this class are thread-safe.  See {@link
# PreparedStatement PreparedStatement} for more information.
# <p>
# Stability: Added in v1.00, stability guaranteed for v1.xx.
#
# @param user   The username to connect to the PostgreSQL server with.  This
# parameter is required.
#
# @keyparam host   The hostname of the PostgreSQL server to connect with.
# Providing this parameter is necessary for TCP/IP connections.  One of either
# host, or unix_sock, must be provided.
#
# @keyparam unix_sock   The path to the UNIX socket to access the database
# through, for example, '/tmp/.s.PGSQL.5432'.  One of either unix_sock or host
# must be provided.  The port parameter will have no affect if unix_sock is
# provided.
#
# @keyparam port   The TCP/IP port of the PostgreSQL server instance.  This
# parameter defaults to 5432, the registered and common port of PostgreSQL
# TCP/IP servers.
#
# @keyparam database   The name of the database instance to connect with.  This
# parameter is optional, if omitted the PostgreSQL server will assume the
# database name is the same as the username.
#
# @keyparam password   The user password to connect to the server with.  This
# parameter is optional.  If omitted, and the database server requests password
# based authentication, the connection will fail.  On the other hand, if this
# parameter is provided and the database does not request password
# authentication, then the password will not be used.
#
# @keyparam socket_timeout  Socket connect timeout measured in seconds.
# Defaults to 60 seconds.
#
# @keyparam ssl     Use SSL encryption for TCP/IP socket.  Defaults to False.
class Connection(Cursor):
    def __init__(self, user, host=None, unix_sock=None, port=5432, database=None, password=None, socket_timeout=60, ssl=False):
        self._row_desc = None
        try:
            self.c = protocol.Connection(unix_sock=unix_sock, host=host, port=port, socket_timeout=socket_timeout, ssl=ssl)
            self.c.authenticate(user, password=password, database=database)
        except socket.error as e:
            raise InterfaceError("communication error", e)
        Cursor.__init__(self, self)
        self._begin = PreparedStatement(self, "BEGIN TRANSACTION")
        self._commit = PreparedStatement(self, "COMMIT TRANSACTION")
        self._rollback = PreparedStatement(self, "ROLLBACK TRANSACTION")

    ##
    # Begins a new transaction.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.
    def begin(self):
        self._begin.execute()

    ##
    # Commits the running transaction.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.
    def commit(self):
        self._commit.execute()

    ##
    # Rolls back the running transaction.
    # <p>
    # Stability: Added in v1.00, stability guaranteed for v1.xx.
    def rollback(self):
        self._rollback.execute()


