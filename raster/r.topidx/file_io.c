#include <string.h>
#include <grass/gis.h>
#include <grass/raster.h>
#include <grass/glocale.h>
#include "global.h"

void getcells(void)
{
    int fd, i, j;
    RASTER_MAP_TYPE data_type;
    CELL *ccell = NULL;
    FCELL *fcell = NULL;
    struct Cell_head inhead;

    if ((fd = Rast_open_cell_old(iname, "")) < 0)
	G_fatal_error(_("Cannot open raster map <%s>"), iname);

    data_type = Rast_get_raster_map_type(fd);
    if (Rast_get_cellhd(iname, "", &inhead) < 0 )
	G_fatal_error(_("Unable to read header of raster map <%s>"), iname);

    if (data_type == CELL_TYPE)
	ccell = (CELL *) G_malloc(sizeof(CELL) * window.cols);
    else if (data_type == FCELL_TYPE)
	fcell = (FCELL *) G_malloc(sizeof(FCELL) * window.cols);

    cell = (DCELL **) G_malloc(sizeof(DCELL *) * window.rows);
    atb = (DCELL **) G_malloc(sizeof(DCELL *) * window.rows);
    a = (DCELL **) G_malloc(sizeof(DCELL *) * window.rows);

    if (window.ew_res < inhead.ew_res || window.ns_res < inhead.ns_res)
        G_fatal_error(_("Current region resolution [%.2fx%.2f] lower than input map resolution [%.2fx%.2f]! Needs to be at least identical or the current region resolution lower than the input map resolution"), window.ew_res, window.ns_res, inhead.ew_res, inhead.ns_res);

    G_important_message(_("Reading elevation map..."));

    for (i = 0; i < window.rows; i++) {
	G_percent(i, window.rows, 2);

	cell[i] = (DCELL *) G_malloc(sizeof(DCELL) * window.cols);
	atb[i] = (DCELL *) G_malloc(sizeof(DCELL) * window.cols);
	a[i] = (DCELL *) G_malloc(sizeof(DCELL) * window.cols);

	if (data_type == CELL_TYPE) {
	    if (Rast_get_c_row(fd, ccell, i) < 0) {
		Rast_close(fd);
	    }
	    for (j = 0; j < window.cols; j++) {
		if (Rast_is_c_null_value(&ccell[j]))
		    Rast_set_d_null_value(&cell[i][j], 1);
		else
		    cell[i][j] = (DCELL) ccell[j];
	    }
	}
	else if (data_type == FCELL_TYPE) {
	    if (Rast_get_f_row(fd, fcell, i) < 0) {
		Rast_close(fd);
	    }
	    for (j = 0; j < window.cols; j++) {
		if (Rast_is_f_null_value(&fcell[j]))
		    Rast_set_d_null_value(&cell[i][j], 1);
		else
		    cell[i][j] = (DCELL) fcell[j];
	    }
	}
	else if (Rast_get_d_row(fd, cell[i], i) < 0) {
	    Rast_close(fd);
	    G_fatal_error(_("Unable to read raster map <%s> row %d"), iname,
			  i);
	}
    }
    if (data_type == CELL_TYPE)
	G_free(ccell);
    else if (data_type == FCELL_TYPE)
	G_free(fcell);
    G_percent(i, window.rows, 2);
    Rast_close(fd);
}


void putcells(void)
{
    int fd, i;
    struct History history;

    if ((fd = Rast_open_raster_new(oname, DCELL_TYPE)) < 0)
	G_fatal_error(_("Cannot create raster map <%s>"), oname);

    G_important_message(_("Writing topographic index map..."));

    for (i = 0; i < window.rows; i++) {
	G_percent(i, window.rows, 2);
	Rast_put_d_raster_row(fd, atb[i]);
    }
    G_percent(i, window.rows, 2);
    Rast_close(fd);

    Rast_short_history(oname, "raster", &history);
    strncpy(history.datsrc_1, iname, RECORD_LEN);
    history.datsrc_1[RECORD_LEN - 1] = '\0';	/* strncpy() doesn't null terminate if maxfill */
    Rast_write_history(oname, &history);
}
